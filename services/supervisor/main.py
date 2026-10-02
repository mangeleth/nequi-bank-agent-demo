"""Supervisor service: POST /v1/disputes/triage.

Order of work for each request (ADR-0013, ADR-0015):
  1. Authenticate: verify the JWT.
  2. Deduplicate: claim the key sha256(user_id, transaction_id). A duplicate gets the stored
     result, or "already being processed", and goes no further.
  3. Authorize in code: the disputed transaction must belong to the caller (no model call yet).
  4. Run the supervisor graph with a hard recursion limit, traced to Langfuse.
  5. Store the result under the key, and return what was decided, the path taken, and a link
     to the trace.

Run locally:  make run-supervisor
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from langchain_core.language_models import BaseChatModel
from langgraph.errors import GraphRecursionError

from services.supervisor.clients import HttpSpecialists, Specialists, SpecialistUnavailable
from services.supervisor.dedup import DisputeGate, GateUnavailable, build_gate, dispute_key
from services.supervisor.graph import RECURSION_LIMIT, TriageContext, TriageState, build_graph
from services.supervisor.messages import customer_message
from shared.auth import AuthError, AuthSettings, CallerIdentity, bearer_token, verify_token
from shared.refund_policy import RefundPolicyConfig
from shared.schemas import Decision, DisputeRequest, DisputeStatus, TriageResult
from shared.tracing import Tracing, build_tracing

log = logging.getLogger("supervisor")


def _status(state: TriageState) -> DisputeStatus:
    if state.get("escalation_reason"):
        return DisputeStatus.PENDING_HUMAN_APPROVAL  # human operations take over
    if (approval := state.get("approval")) is not None:
        return approval.status  # resolved if auto-approved, otherwise pending a human
    verdict = state.get("verdict")
    if verdict is not None and verdict.decision == Decision.NO_ACTION:
        return DisputeStatus.RESOLVED
    return DisputeStatus.PENDING_HUMAN_APPROVAL  # escalate_fraud: fraud operations take over


def _result(dispute: DisputeRequest, state: TriageState, trace_url: str | None) -> TriageResult:
    verdict = state.get("verdict")
    return TriageResult(
        dispute_id=verdict.dispute_id if verdict else uuid4(),
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


def create_app(
    *,
    auth: AuthSettings | None = None,
    model: BaseChatModel | None = None,
    specialists: Specialists | None = None,
    tracing: Tracing | None = None,
    policy: RefundPolicyConfig | None = None,
    gate: DisputeGate | None = None,
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
        app.state.graph = build_graph(model or build_chat_model())
        yield
        app.state.tracing.shutdown()
        if http is not None:
            await http.aclose()

    app = FastAPI(title="Dispute Triage Supervisor", version="1.0.0", lifespan=lifespan)

    def caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> tuple[CallerIdentity, str]:
        try:
            token = bearer_token(authorization)
            return verify_token(token, request.app.state.auth), token
        except AuthError as exc:
            log.warning("authentication failed: %s", exc)
            raise HTTPException(401, "invalid or missing token", {"WWW-Authenticate": "Bearer"}) from exc

    async def _run_triage(dispute: DisputeRequest, identity: CallerIdentity, token: str, state) -> TriageResult:
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
            },
        }
        context = TriageContext(
            caller=identity, token=token, specialists=state.specialists, policy=state.policy, trace_id=trace_id
        )
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
        return _result(dispute, final, state.tracing.url(trace_id))

    @app.post("/v1/disputes/triage", response_model=TriageResult)
    async def triage(
        dispute: DisputeRequest, request: Request, auth_: Annotated[tuple[CallerIdentity, str], Depends(caller)]
    ):
        identity, token = auth_
        state = request.app.state

        # The gate: one dispute per customer and transaction. A duplicate stops here, before the
        # ownership lookup and before any model call. If the store is down we fail closed.
        key = dispute_key(identity.user_id, dispute.transaction_id)
        try:
            claim = await state.gate.claim(key)
        except GateUnavailable as exc:
            log.error("dispute gate unavailable: %s", exc)
            raise HTTPException(503, "dispute intake is temporarily unavailable", {"Retry-After": "10"}) from exc
        if claim.result is not None:
            return Response(claim.result, media_type="application/json", headers={"Idempotent-Replay": "true"})
        if claim.in_progress:
            raise HTTPException(409, "this dispute is already being processed", {"Retry-After": "5"})

        # This request holds the key. It must end by storing a result or by giving the key back.
        try:
            # Authorization in code, before any model call.
            if not await state.specialists.owns_transaction(identity, dispute.transaction_id):
                raise HTTPException(404, "transaction not found")
        except SpecialistUnavailable as exc:
            await _give_back(state.gate, key)
            raise HTTPException(503, "core systems unavailable") from exc
        except HTTPException:
            await _give_back(state.gate, key)
            raise

        result = await _run_triage(dispute, identity, token, state)
        try:
            await state.gate.complete(key, result.model_dump_json())
        except GateUnavailable as exc:
            # The triage is done; the claim expires on its own. Duplicates may run again after that.
            log.error("could not store the result of %s: %s", dispute.transaction_id, exc)
        return result

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        # Not ready without the gate's store: a replica that cannot deduplicate takes no traffic.
        ready = await request.app.state.gate.ping() and await request.app.state.specialists.ready()
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
