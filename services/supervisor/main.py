"""Supervisor service: the intake API.

    POST /v1/disputes           accept a dispute: 202 Accepted and a dispute ID, at once
    GET  /v1/disputes/{id}      where the dispute stands, and the result once the run has finished

Order of work for POST (ADR-0013, ADR-0015, ADR-0016, ADR-0018):
  1. Authenticate: verify the JWT.
  2. Deduplicate: claim the key sha256(user_id, transaction_id) in Redis (the fast path).
     A duplicate is pointed to the dispute that already exists and goes no further.
  3. Authorize in code: the disputed transaction must belong to the caller.
  4. Store the dispute in PostgreSQL (the unique key there is the guarantee).
  5. Put the dispute ID on the queue and answer 202.

A worker takes it from the queue and runs the triage (services/supervisor/triage.py). With the
in-memory queue (tests, local runs) that worker runs inside this process; in the cluster it is
a separate deployment and this service only accepts disputes.

Run locally:  make run-supervisor
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from services.supervisor import review
from services.supervisor.clients import HttpSpecialists, Specialists, SpecialistUnavailable
from services.supervisor.dedup import DisputeGate, GateUnavailable, build_gate, dispute_key
from services.supervisor.graph import RECURSION_LIMIT, build_graph
from services.supervisor.messages import NEEDS_PERSON, RECEIVED
from services.supervisor.payments import REFUND_MAX_DELIVERIES, RefundPayer
from services.supervisor.queue import DisputeQueue, InMemoryQueue, ServiceBusQueue
from services.supervisor.store import DisputeRecord, DisputeStore, open_store
from services.supervisor.triage import RETRY_DELAY_SECONDS, TriageRunner
from shared.auth import (
    AuthError,
    AuthSettings,
    CallerIdentity,
    ReviewerIdentity,
    bearer_token,
    verify_reviewer_token,
    verify_token,
)
from shared.delegation import DelegationSettings, TokenSigner, build_signer
from shared.refund_policy import RefundPolicyConfig
from shared.schemas import DisputeRequest, DisputeView
from shared.tracing import Tracing, build_tracing

log = logging.getLogger("supervisor")

# A run with no status change for this long is treated as dead. It must be longer than the
# queue's lock multiplied by its deliveries, so a dispute waiting for redelivery is not failed.
STUCK_AFTER_SECONDS = 900
SWEEP_EVERY_SECONDS = 60


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


class ReviewRequest(BaseModel):
    """A reviewer's decision. For approve, the amount is NOT here: it is read from the ledger."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str = Field(min_length=5, max_length=500)


class FollowUpDone(BaseModel):
    """What customer service did about it, for the audit trail."""

    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=5, max_length=500)


def build_queue() -> DisputeQueue:
    """Choose the queue from QUEUE_BACKEND: `memory` (default; the worker runs in this process)
    or `servicebus` (the worker is a separate deployment)."""
    backend = os.environ.get("QUEUE_BACKEND", "memory").strip()
    if backend == "memory":
        return InMemoryQueue()
    if backend == "servicebus":
        return ServiceBusQueue(os.environ["SERVICEBUS_NAMESPACE"], os.environ["SERVICEBUS_QUEUE"])
    raise ValueError(f"unknown QUEUE_BACKEND={backend!r} (supported: memory, servicebus)")


def create_app(
    *,
    auth: AuthSettings | None = None,
    model: BaseChatModel | None = None,
    specialists: Specialists | None = None,
    tracing: Tracing | None = None,
    policy: RefundPolicyConfig | None = None,
    gate: DisputeGate | None = None,
    store: DisputeStore | None = None,
    queue: DisputeQueue | None = None,
    signer: TokenSigner | None = None,
    delegation: DelegationSettings | None = None,
    recursion_limit: int = RECURSION_LIMIT,
    retry_delay_seconds: float = RETRY_DELAY_SECONDS,
    shutdown_grace_seconds: float = 30.0,
    judge_jobs=None,
) -> FastAPI:
    """Build the app. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if model is None:
            from shared.llm import build_chat_model  # imported here so tests need no Azure setup

        http = None
        if specialists is None:
            http = httpx.AsyncClient(timeout=60)  # an agent run takes several seconds
        state = app.state
        state.auth = auth or AuthSettings.from_env()
        state.specialists = specialists or HttpSpecialists(
            http,
            fraud_url=os.environ["FRAUD_AGENT_URL"],
            ledger_url=os.environ["LEDGER_AGENT_URL"],
            core_url=os.environ["CORE_SYSTEMS_URL"],
        )
        state.gate = gate or build_gate()
        state.store, pool = (store, None) if store is not None else await open_store()
        state.queue = queue or build_queue()
        # Where a reviewer's approval goes to be paid (ADR-0027). This process may only SEND to it.
        state.refunds = None
        if isinstance(state.queue, ServiceBusQueue) and os.environ.get("REFUNDS_QUEUE", "").strip():
            state.refunds = ServiceBusQueue(os.environ["SERVICEBUS_NAMESPACE"], os.environ["REFUNDS_QUEUE"],
                                            max_deliveries=REFUND_MAX_DELIVERIES)

        background = [asyncio.create_task(_sweep(state.store))]
        worker_tracing = None
        if isinstance(state.queue, InMemoryQueue):
            # No separate worker exists for an in-memory queue, so this process is the worker too.
            signing, settings = (signer, delegation) if signer is not None else build_signer()
            worker_tracing = tracing or build_tracing()
            # ... and the refund payer, with its own in-memory queue and delivery limit.
            refunds = InMemoryQueue(max_deliveries=REFUND_MAX_DELIVERIES)
            state.refunds = refunds
            payer = RefundPayer(store=state.store, queue=refunds, specialists=state.specialists,
                                retry_delay_seconds=retry_delay_seconds, **RefundPayer.settings_from_env())
            runner = TriageRunner(
                judge_jobs=judge_jobs,
                store=state.store, queue=state.queue, refunds=refunds, graph=build_graph(model or build_chat_model()),
                specialists=state.specialists, policy=policy or RefundPolicyConfig.from_env(),
                tracing=worker_tracing, signer=signing, delegation=settings,
                recursion_limit=recursion_limit, retry_delay_seconds=retry_delay_seconds,
                shutdown_grace_seconds=shutdown_grace_seconds,
            )
            background.append(asyncio.create_task(runner.run_forever()))
            background.append(asyncio.create_task(payer.run_forever()))
        yield
        for task in background:
            task.cancel()
        for task in background:
            with suppress(asyncio.CancelledError):
                await task
        if worker_tracing is not None:
            worker_tracing.shutdown()
        for bus in (state.queue, state.refunds):
            if isinstance(bus, ServiceBusQueue):
                await bus.close()
        if pool is not None:
            await pool.close()
        if http is not None:
            await http.aclose()

    app = FastAPI(title="Dispute Triage Supervisor", version="3.0.0", lifespan=lifespan)

    async def _sweep(dispute_store: DisputeStore) -> None:
        """Fail runs that never finished, so no dispute stays 'running' forever."""
        while True:
            await asyncio.sleep(SWEEP_EVERY_SECONDS)
            try:
                failed = await dispute_store.fail_stuck(STUCK_AFTER_SECONDS, NEEDS_PERSON)
                if failed:
                    log.error("%d run(s) did not finish in time and were marked for a person", failed)
            except Exception:
                log.exception("sweep failed; will try again")

    def caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> CallerIdentity:
        try:
            return verify_token(bearer_token(authorization), request.app.state.auth)
        except AuthError as exc:
            log.warning("authentication failed: %s", exc)
            raise HTTPException(401, "invalid or missing token", {"WWW-Authenticate": "Bearer"}) from exc

    Caller = Annotated[CallerIdentity, Depends(caller)]

    @app.post("/v1/disputes", status_code=202, response_model=DisputeView)
    async def submit_dispute(dispute: DisputeRequest, request: Request, response: Response, identity: Caller):
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

        # Hand the work to the queue. The message is only the ID: no customer data, no token.
        try:
            await state.queue.send(record.dispute_id)
        except Exception:
            # The dispute exists and must not be lost or left waiting: it goes to a person.
            log.exception("could not queue dispute %s", record.dispute_id)
            await state.store.fail(record.dispute_id, NEEDS_PERSON, "the dispute could not be queued")
            record = await state.store.get(record.dispute_id, identity.user_id) or record
        response.headers["Location"] = f"/v1/disputes/{record.dispute_id}"
        return record.view()

    @app.get("/v1/disputes/{dispute_id}", response_model=DisputeView)
    async def get_dispute(dispute_id: UUID, request: Request, identity: Caller):
        # Scoped to the caller in the query itself: someone else's dispute is "not found".
        record = await request.app.state.store.get(dispute_id, identity.user_id)
        if record is None:
            raise HTTPException(404, "dispute not found")
        return record.view()

    # --- Review by a person (ADR-0027): every route needs a REVIEWER token -------------------------

    def reviewer(request: Request, authorization: Annotated[str | None, Header()] = None) -> ReviewerIdentity:
        try:
            return verify_reviewer_token(bearer_token(authorization), request.app.state.auth)
        except AuthError as exc:
            log.warning("review access refused: %s", exc)
            raise HTTPException(401, "a reviewer token is required", {"WWW-Authenticate": "Bearer"}) from exc

    Reviewer = Annotated[ReviewerIdentity, Depends(reviewer)]

    async def _review_item(state, record) -> dict:
        return {"dispute": record.view().model_dump(mode="json"), "customer_id": record.user_id,
                "request": record.request, "attempts": record.attempts,
                "judgement": jsonable_encoder(await state.store.judgement(record.dispute_id))}

    @app.get("/v1/reviews/queue")
    async def review_queue(request: Request, who: Reviewer) -> list[dict]:
        """Disputes waiting for a person, oldest first, each with the judge's verdict if any."""
        state = request.app.state
        return [await _review_item(state, record) for record in await state.store.review_queue()]

    @app.get("/v1/reviews/disputes/{dispute_id}")
    async def review_dispute(dispute_id: UUID, request: Request, who: Reviewer) -> dict:
        state = request.app.state
        record = await state.store.load(dispute_id)
        if record is None:
            raise HTTPException(404, "dispute not found")
        return await _review_item(state, record) | {"events": jsonable_encoder(await state.store.events(dispute_id))}

    @app.post("/v1/reviews/disputes/{dispute_id}/decision", response_model=DisputeView)
    async def review_decision(dispute_id: UUID, body: ReviewRequest, request: Request, who: Reviewer):
        state = request.app.state
        record = await state.store.load(dispute_id)
        if record is None:
            raise HTTPException(404, "dispute not found")
        if body.decision == "approve" and state.refunds is None:
            raise HTTPException(503, "approvals cannot be paid: no refunds queue is configured")
        try:
            await review.decide(store=state.store, specialists=state.specialists, refunds=state.refunds,
                                record=record, reviewer=who, decision=body.decision, note=body.note)
        except review.ReviewRefused as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except SpecialistUnavailable as exc:
            raise HTTPException(503, "the ledger is unavailable; try again") from exc
        log.info("dispute %s: %s by %s", dispute_id, body.decision, who.reviewer_id)
        return (await state.store.load(dispute_id)).view()

    @app.get("/v1/reviews/follow-ups")
    async def follow_ups(request: Request, who: Reviewer) -> list[dict]:
        """Disputes the LLM judge sent to customer service (ADR-0026), oldest first."""
        state = request.app.state
        items = []
        for follow_up in await state.store.follow_ups():
            record = await state.store.load(follow_up["dispute_id"])
            items.append(jsonable_encoder(follow_up) | await _review_item(state, record))
        return items

    @app.post("/v1/reviews/follow-ups/{dispute_id}/resolve")
    async def resolve_follow_up(dispute_id: UUID, body: FollowUpDone, request: Request, who: Reviewer) -> dict:
        if not await request.app.state.store.resolve_follow_up(dispute_id, who.reviewer_id, body.note):
            raise HTTPException(409, "there is no open follow-up for this dispute")
        log.info("follow-up of dispute %s done by %s", dispute_id, who.reviewer_id)
        return {"dispute_id": str(dispute_id), "resolved_by": who.reviewer_id}

    @app.get("/v1/reviews/judgements")
    async def recent_judgements(request: Request, who: Reviewer) -> list[dict]:
        """The LLM judge's most recent verdicts (ADR-0026), for the reviewers' dashboard."""
        return jsonable_encoder(await request.app.state.store.judgements(50))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        # Not ready without the gate, the dispute store, and the queue: such a replica takes no
        # traffic. The agents are NOT checked: if they are down, disputes are still accepted and
        # wait in the queue, which is the point of having one.
        state = request.app.state
        ready = await state.gate.ping() and await state.store.ping() and await state.queue.ping()
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
