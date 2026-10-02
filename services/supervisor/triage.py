"""The worker side: take a dispute from the queue and run its triage (ADR-0018).

One delivery is handled like this:

    load the dispute from PostgreSQL          (the message is only an ID)
    queued -> running                         (or take over a run whose worker died)
    issue the supervisor's own token          (ADR-0017)
    run the graph, bounded and traced         (ADR-0013)
    running -> finished                       (the decision is now saved)
    if a refund was approved: send its ID to the refunds queue; the refund payer pays it
                                              (ADR-0020, ADR-0021, services/supervisor/payments.py)
    complete the message

If anything fails on the way:
    not the last delivery -> wait a moment (with jitter), abandon: the queue offers it again
    the last delivery     -> the dispute goes to a person, the message to the dead-letter queue

The work is safe to repeat (idempotent): a status only changes from the status it is expected to
be in, so a message delivered twice cannot finish a dispute twice.

Paying is a separate step that works from the SAVED approval, never from a new run of the graph.
If a worker dies after the decision was saved, the next delivery finds a finished dispute that is
still `refund_approved` and only queues its payment: the model is not asked again, so it cannot
change its mind about money that may already have moved. A payment queued twice is harmless: the
payer skips a dispute that is no longer awaiting payment, and the ledger pays a key only once.
"""

import asyncio
import logging
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from langgraph.errors import GraphRecursionError

from services.supervisor.clients import Specialists
from services.supervisor.graph import RECURSION_LIMIT, TriageContext, TriageState
from services.supervisor.incident_path import decide_known_incident, with_incident
from services.supervisor.messages import INVESTIGATING, NEEDS_PERSON, customer_message
from services.supervisor.queue import MAX_DELIVERIES, Delivery, DisputeQueue
from services.supervisor.store import DisputeRecord, DisputeStore
from shared.auth import CallerIdentity
from shared.delegation import DelegationSettings, TokenSigner, issue_delegated_token
from shared.refund_policy import RefundPolicyConfig
from shared.schemas import (
    Decision,
    DisputeRequest,
    DisputeStatus,
    ExecutionStatus,
    TriageResult,
)
from shared.tracing import Tracing

log = logging.getLogger("triage")

MAX_CONCURRENT_TRIAGES = 4  # per worker: a burst of disputes waits in the queue, not in memory
RETRY_DELAY_SECONDS = 2.0  # base wait before a failed delivery is offered again


def retry_delay(base: float = RETRY_DELAY_SECONDS) -> float:
    """Base delay with jitter: a random 50-150% of it, so failures that happened together do
    not all retry at the same instant and overload the same dependency again."""
    return base * random.uniform(0.5, 1.5)


def _status(state: TriageState) -> DisputeStatus:
    if state.get("escalation_reason"):
        return DisputeStatus.PENDING_HUMAN_APPROVAL  # human operations take over
    if (approval := state.get("approval")) is not None:
        return approval.status  # refund approved, or pending a human
    verdict = state.get("verdict")
    if verdict is not None and verdict.decision == Decision.NO_ACTION:
        return DisputeStatus.CLOSED_NO_REFUND
    return DisputeStatus.PENDING_HUMAN_APPROVAL  # escalate_fraud: fraud operations take over


def build_result(dispute_id: UUID, dispute: DisputeRequest, state: TriageState, trace_url: str | None) -> TriageResult:
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


@dataclass
class TriageRunner:
    """Everything a worker needs. The API process and the standalone worker build the same one."""

    store: DisputeStore
    queue: DisputeQueue
    refunds: DisputeQueue  # approved refunds go here; the refund payer pays them
    graph: object
    specialists: Specialists
    policy: RefundPolicyConfig
    tracing: Tracing
    signer: TokenSigner
    delegation: DelegationSettings
    recursion_limit: int = RECURSION_LIMIT
    retry_delay_seconds: float = RETRY_DELAY_SECONDS
    shutdown_grace_seconds: float = 30.0

    async def run_graph(self, record: DisputeRecord) -> TriageResult:
        dispute = DisputeRequest.model_validate(record.request)
        identity = CallerIdentity(
            user_id=record.user_id, token_id=f"dispute-{record.dispute_id}",
            expires_at=datetime.now(UTC) + timedelta(seconds=self.delegation.lifetime_seconds),
        )
        # A confirmed incident already explains this transaction? Then there is nothing to
        # investigate: decide by code, with zero model calls (ADR-0022).
        known = await decide_known_incident(self.specialists, identity, record.dispute_id, dispute, self.policy)
        if known is not None:
            incident, state = known
            log.info("dispute %s decided by incident %s without a model", record.dispute_id, incident.incident_id)
            result = build_result(record.dispute_id, dispute, state, trace_url=None)
            return result.model_copy(update={"incident": incident,
                                             "customer_message": with_incident(result.customer_message, incident)})

        # The customer was verified at submission. The agents get our own token for this
        # customer and this transaction; no customer login token exists here at all.
        token = await issue_delegated_token(
            self.signer, self.delegation, user_id=record.user_id,
            transaction_id=dispute.transaction_id, dispute_id=str(record.dispute_id),
        )
        callbacks, trace_id = self.tracing.start()
        config = {
            "recursion_limit": self.recursion_limit,  # hard stop, whatever the graph and the model do
            "callbacks": callbacks,
            "run_name": "dispute-triage",
            "metadata": {
                "langfuse_user_id": record.user_id,
                "langfuse_session_id": str(record.dispute_id),
                "langfuse_tags": ["dispute-triage", dispute.reason.value],
                "transaction_id": dispute.transaction_id,
                "dispute_id": str(record.dispute_id),
            },
        }
        context = TriageContext(dispute_id=record.dispute_id, caller=identity, token=token,
                                specialists=self.specialists, policy=self.policy, trace_id=trace_id)
        try:
            final = await self.graph.ainvoke({"dispute": dispute}, config=config, context=context)
        except GraphRecursionError:
            # Not a temporary failure: retrying would loop again. Straight to a person.
            reason = f"the graph exceeded its hard limit of {self.recursion_limit} steps"
            log.error("triage of %s stopped: %s", dispute.transaction_id, reason)
            final = {"escalation_reason": reason, "steps": [f"escalate: {reason}"]}
        return build_result(record.dispute_id, dispute, final, self.tracing.url(trace_id))

    async def handle(self, delivery: Delivery) -> None:
        """Process one delivery. Never raises: every outcome ends in complete, abandon, or dead-letter."""
        dispute_id = delivery.dispute_id
        try:
            record = await self.store.load(dispute_id)
            if (record is not None and record.execution_status == ExecutionStatus.FINISHED
                    and record.business_status == DisputeStatus.REFUND_APPROVED):
                # The decision was saved but its payment may not have been queued (the worker died
                # in between). Queue it from the saved approval: no new run of the graph.
                await self.refunds.send(dispute_id)
                await self.queue.complete(delivery)
                return
            if record is None or record.execution_status in (ExecutionStatus.FINISHED, ExecutionStatus.FAILED):
                await self.queue.complete(delivery)  # nothing left to do: a duplicate or stale message
                return
            # First delivery: queued -> running. A later delivery means the previous worker failed
            # or died, so this one may take over a dispute that is still marked running.
            if not await self.store.start(dispute_id, INVESTIGATING, takeover=delivery.delivery_count > 1,
                                          max_attempts=MAX_DELIVERIES):
                await self.queue.complete(delivery)
                return
            result = await self.run_graph(record)
            await self.store.finish(dispute_id, result)  # the decision is saved before any money moves
            if result.status == DisputeStatus.REFUND_APPROVED:
                await self.refunds.send(dispute_id)  # paid by the refund payer, at its own pace
            await self.queue.complete(delivery)
        except Exception:
            log.exception("delivery %d of dispute %s failed", delivery.delivery_count, dispute_id)
            await self._give_up_or_retry(delivery)

    async def _give_up_or_retry(self, delivery: Delivery) -> None:
        with suppress(Exception):  # if even this fails, the lock expires and the queue redelivers
            if delivery.is_last:
                record = await self.store.load(delivery.dispute_id)
                if record is not None and record.business_status == DisputeStatus.REFUND_APPROVED \
                        and record.execution_status == ExecutionStatus.FINISHED:
                    # Approved, but its payment could not be queued. Nothing was paid; a person
                    # takes it, so the dispute is not left looking approved forever.
                    from services.supervisor.payments import to_a_person

                    await to_a_person(self.store, record,
                                      reason="the approved refund could not be queued for payment",
                                      step="pay: could not be queued -> a person",
                                      note="payment could not be queued")
                else:
                    await self.store.fail(delivery.dispute_id, NEEDS_PERSON,
                                          f"the run failed on delivery {delivery.delivery_count} of {MAX_DELIVERIES}")
                await self.queue.dead_letter(delivery, "the run failed on its last delivery")
            else:
                await asyncio.sleep(retry_delay(self.retry_delay_seconds))
                await self.queue.abandon(delivery)

    async def run_forever(self) -> None:
        """Consume the queue, at most MAX_CONCURRENT_TRIAGES at a time."""
        slots = asyncio.Semaphore(MAX_CONCURRENT_TRIAGES)
        running: set[asyncio.Task] = set()

        async def one(delivery: Delivery) -> None:
            try:
                await self.handle(delivery)
            finally:
                slots.release()

        deliveries = self.queue.receive().__aiter__()
        try:
            while True:
                await slots.acquire()  # take a message only when there is room to work on it
                try:
                    delivery = await deliveries.__anext__()
                except StopAsyncIteration:
                    slots.release()
                    break
                task = asyncio.create_task(one(delivery))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            if running:
                # Shutting down: give the runs in progress time to finish. Any still running after
                # that are cancelled; their messages were not completed, so the queue redelivers them.
                _, unfinished = await asyncio.wait(running, timeout=self.shutdown_grace_seconds)
                for task in unfinished:
                    task.cancel()
