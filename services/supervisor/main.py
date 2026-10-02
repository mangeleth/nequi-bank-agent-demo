"""Supervisor service.

    POST /v1/disputes           accept a dispute: 202 Accepted and a dispute ID, at once
    GET  /v1/disputes/{id}      where the dispute stands, and the result once the run has finished

Order of work for POST (ADR-0013, ADR-0015, ADR-0016):
  1. Authenticate: verify the JWT.
  2. Deduplicate: claim the key sha256(user_id, transaction_id) in Redis (the fast path).
     A duplicate is pointed to the dispute that already exists and goes no further.
  3. Authorize in code: the disputed transaction must belong to the caller.
  4. Store the dispute in PostgreSQL (the unique key there is the guarantee).
  5. Start the triage in the background and answer 202.

The supervisor trusts only the customer identity provider. When a run starts it issues its own
short-lived token for the agents, valid for that customer and that transaction (ADR-0017).

The triage itself is three conditional status changes around the graph:
    queued -> running        (store.start)
    ...the supervisor graph, with a hard recursion limit, traced to Langfuse...
    running -> finished      (store.finish), or -> failed (store.fail) if anything goes wrong

Run locally:  make run-supervisor
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from langgraph.errors import GraphRecursionError

from services.supervisor.clients import HttpSpecialists, Specialists, SpecialistUnavailable
from services.supervisor.dedup import DisputeGate, GateUnavailable, build_gate, dispute_key
from services.supervisor.graph import RECURSION_LIMIT, TriageContext, TriageState, build_graph
from services.supervisor.messages import INVESTIGATING, NEEDS_PERSON, RECEIVED, customer_message
from services.supervisor.store import DisputeRecord, DisputeStore, open_store
from shared.auth import AuthError, AuthSettings, CallerIdentity, bearer_token, verify_token
from shared.delegation import DelegationSettings, TokenSigner, build_signer, issue_delegated_token
from shared.refund_policy import RefundPolicyConfig
from shared.schemas import Decision, DisputeRequest, DisputeStatus, DisputeView, TriageResult
from shared.tracing import Tracing, build_tracing

log = logging.getLogger("supervisor")

MAX_CONCURRENT_TRIAGES = 4  # per replica: a burst of disputes waits its turn instead of all running at once
STUCK_AFTER_SECONDS = 300  # a run with no status change for this long is treated as dead
SWEEP_EVERY_SECONDS = 60


def _status(state: TriageState) -> DisputeStatus:
    if state.get("escalation_reason"):
        return DisputeStatus.PENDING_HUMAN_APPROVAL  # human operations take over
    if (approval := state.get("approval")) is not None:
        return approval.status  # refund approved, or pending a human
    verdict = state.get("verdict")
    if verdict is not None and verdict.decision == Decision.NO_ACTION:
        return DisputeStatus.CLOSED_NO_REFUND
    return DisputeStatus.PENDING_HUMAN_APPROVAL  # escalate_fraud: fraud operations take over


def _result(dispute_id: UUID, dispute: DisputeRequest, state: TriageState, trace_url: str | None) -> TriageResult:
    verdict = state.get("verdict")
    return TriageResult(
        dispute_id=dispute_id,
        transaction_id=dispute.transaction_id,
        status=_status(state),
        verdict=verdict,
        approval=state.get("approval"),
        fraud=state.get("fraud"),
        ledger=state.get("ledger"),
        customer_message=customer_message(
            ledger=state.get("ledger"), verdict=verdict, approval=state.get("approval"),
            escalated=bool(state.get("escalation_reason")),
        ),
        escalation_reason=state.get("escalation_reason"),
        steps=state.get("steps", []),
        trace_url=trace_url,
    )


async def _give_back(gate: DisputeGate, key: str) -> None:
    try:
        await gate.release(key)
    except GateUnavailable as exc:
        log.error("could not release a dispute key (it will expire): %s", exc)


def _replay(record: DisputeRecord) -> JSONResponse:
    """Answer a duplicate with the dispute that already exists, as it stands now."""
    return JSONResponse(
        record.view().model_dump(mode="json"),
        headers={"Idempotent-Replay": "true", "Location": f"/v1/disputes/{record.dispute_id}"},
    )


def create_app(
    *,
    auth: AuthSettings | None = None,
    model: BaseChatModel | None = None,
    specialists: Specialists | None = None,
    tracing: Tracing | None = None,
    policy: RefundPolicyConfig | None = None,
    gate: DisputeGate | None = None,
    store: DisputeStore | None = None,
    signer: TokenSigner | None = None,
    delegation: DelegationSettings | None = None,
    recursion_limit: int = RECURSION_LIMIT,
) -> FastAPI:
    """Build the app. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if model is None:
            from shared.llm import build_chat_model  # imported here so tests need no Azure setup

        http = None
        if specialists is None:
            http = httpx.AsyncClient(timeout=60)  # an agent run takes several seconds
        app.state.auth = auth or AuthSettings.from_env()
        app.state.specialists = specialists or HttpSpecialists(
            http,
            fraud_url=os.environ["FRAUD_AGENT_URL"],
            ledger_url=os.environ["LEDGER_AGENT_URL"],
            core_url=os.environ["CORE_SYSTEMS_URL"],
        )
        app.state.policy = policy or RefundPolicyConfig.from_env()
        app.state.tracing = tracing or build_tracing()
        app.state.gate = gate or build_gate()
        app.state.store, pool = (store, None) if store is not None else await open_store()
        app.state.signer, app.state.delegation = (signer, delegation) if signer is not None else build_signer()
        app.state.graph = build_graph(model or build_chat_model())
        app.state.slots = asyncio.Semaphore(MAX_CONCURRENT_TRIAGES)
        app.state.runs = set()  # background triages, kept so they are not garbage-collected
        sweeper = asyncio.create_task(_sweep(app.state.store))
        yield
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        if app.state.runs:  # let running triages finish before the process exits
            await asyncio.wait(app.state.runs, timeout=30)
        app.state.tracing.shutdown()
        if pool is not None:
            await pool.close()
        if http is not None:
            await http.aclose()

    app = FastAPI(title="Dispute Triage Supervisor", version="2.0.0", lifespan=lifespan)

    async def _sweep(dispute_store: DisputeStore) -> None:
        """Fail runs whose process died, so no dispute stays 'running' forever."""
        while True:
            await asyncio.sleep(SWEEP_EVERY_SECONDS)
            try:
                failed = await dispute_store.fail_stuck(STUCK_AFTER_SECONDS, NEEDS_PERSON)
                if failed:
                    log.error("%d run(s) did not finish in time and were marked for a person", failed)
            except Exception:
                log.exception("sweep failed; will try again")

    def caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> tuple[CallerIdentity, str]:
        try:
            token = bearer_token(authorization)
            return verify_token(token, request.app.state.auth), token
        except AuthError as exc:
            log.warning("authentication failed: %s", exc)
            raise HTTPException(401, "invalid or missing token", {"WWW-Authenticate": "Bearer"}) from exc

    Caller = Annotated[tuple[CallerIdentity, str], Depends(caller)]

    async def _run_graph(dispute_id: UUID, dispute: DisputeRequest, identity: CallerIdentity, token: str,
                         state) -> TriageResult:
        callbacks, trace_id = state.tracing.start()
        config = {
            "recursion_limit": recursion_limit,  # hard stop, whatever the graph and the model do
            "callbacks": callbacks,
            "run_name": "dispute-triage",
            "metadata": {
                "langfuse_user_id": identity.user_id,
                "langfuse_session_id": identity.token_id,
                "langfuse_tags": ["dispute-triage", dispute.reason.value],
                "transaction_id": dispute.transaction_id,
                "dispute_id": str(dispute_id),
            },
        }
        context = TriageContext(dispute_id=dispute_id, caller=identity, token=token,
                                specialists=state.specialists, policy=state.policy, trace_id=trace_id)
        try:
            final = await state.graph.ainvoke({"dispute": dispute}, config=config, context=context)
        except GraphRecursionError:
            reason = f"the graph exceeded its hard limit of {recursion_limit} steps"
            log.error("triage of %s stopped: %s", dispute.transaction_id, reason)
            final = {"escalation_reason": reason, "steps": [f"escalate: {reason}"]}
        except Exception:
            # Anything unforeseen ends in human review, never in a crash or a silent drop.
            log.exception("triage of %s failed unexpectedly", dispute.transaction_id)
            reason = "an unexpected error stopped the triage"
            final = {"escalation_reason": reason, "steps": [f"escalate: {reason}"]}
        return _result(dispute_id, dispute, final, state.tracing.url(trace_id))

    async def _triage(dispute_id: UUID, dispute: DisputeRequest, identity: CallerIdentity, state) -> None:
        """One background run: queued -> running -> finished, or failed."""
        async with state.slots:
            try:
                if not await state.store.start(dispute_id, INVESTIGATING):
                    return  # no longer queued: another worker took it, or the sweeper failed it
                # The agents get a fresh token issued by us for this customer and this transaction
                # only. The customer's own login token is never forwarded or stored.
                token = await issue_delegated_token(
                    state.signer, state.delegation, user_id=identity.user_id,
                    transaction_id=dispute.transaction_id, dispute_id=str(dispute_id),
                )
                result = await _run_graph(dispute_id, dispute, identity, token, state)
                await state.store.finish(dispute_id, result)
            except Exception:
                log.exception("run for dispute %s failed", dispute_id)
                with suppress(Exception):
                    await state.store.fail(dispute_id, NEEDS_PERSON, "the run failed outside the graph")

    @app.post("/v1/disputes", status_code=202, response_model=DisputeView)
    async def submit_dispute(dispute: DisputeRequest, request: Request, response: Response, auth_: Caller):
        identity, _ = auth_
        state = request.app.state

        # The gate: one dispute per customer and transaction. If its store is down we fail closed.
        key = dispute_key(identity.user_id, dispute.transaction_id)
        try:
            claim = await state.gate.claim(key)
        except GateUnavailable as exc:
            log.error("dispute gate unavailable: %s", exc)
            raise HTTPException(503, "dispute intake is temporarily unavailable", {"Retry-After": "10"}) from exc
        if claim.dispute_id is not None:
            existing = await state.store.get(UUID(claim.dispute_id), identity.user_id)
            if existing is not None:
                return _replay(existing)
        elif claim.in_progress:
            raise HTTPException(409, "this dispute is already being processed", {"Retry-After": "5"})

        # This request holds the key. It must end by creating the dispute or by giving the key back.
        try:
            # Authorization in code, before anything is stored and before any model call.
            if not await state.specialists.owns_transaction(identity, dispute.transaction_id):
                raise HTTPException(404, "transaction not found")
            record, created = await state.store.create(
                dispute_id=uuid4(), dispute_key=key, user_id=identity.user_id, request=dispute,
                customer_message=RECEIVED,
            )
        except HTTPException:
            await _give_back(state.gate, key)
            raise
        except SpecialistUnavailable as exc:
            await _give_back(state.gate, key)
            raise HTTPException(503, "core systems unavailable") from exc
        except Exception as exc:
            log.exception("could not store dispute for %s", dispute.transaction_id)
            await _give_back(state.gate, key)
            raise HTTPException(503, "dispute intake is temporarily unavailable", {"Retry-After": "10"}) from exc

        try:
            await state.gate.complete(key, str(record.dispute_id))
        except GateUnavailable as exc:
            log.error("could not point the key at dispute %s (the database still guards it): %s", record.dispute_id, exc)
        if not created:
            return _replay(record)  # Redis had forgotten this key; the database had not

        run = asyncio.create_task(_triage(record.dispute_id, dispute, identity, state))
        state.runs.add(run)
        run.add_done_callback(state.runs.discard)
        response.headers["Location"] = f"/v1/disputes/{record.dispute_id}"
        return record.view()

    @app.get("/v1/disputes/{dispute_id}", response_model=DisputeView)
    async def get_dispute(dispute_id: UUID, request: Request, auth_: Caller):
        identity, _ = auth_
        # Scoped to the caller in the query itself: someone else's dispute is "not found".
        record = await request.app.state.store.get(dispute_id, identity.user_id)
        if record is None:
            raise HTTPException(404, "dispute not found")
        return record.view()

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        # Not ready without the gate and the dispute store: such a replica takes no traffic.
        state = request.app.state
        ready = await state.gate.ping() and await state.store.ping() and await state.specialists.ready()
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
