"""The worker side: take a dispute from the queue and run its triage (ADR-0018).

One delivery is handled like this:

    load the dispute from PostgreSQL          (the message is only an ID)
    queued -> running                         (or take over a run whose worker died)
    issue the supervisor's own token          (ADR-0017)
    run the graph, bounded and traced         (ADR-0013)
    running -> finished                       (the decision is now saved)
    if a refund was approved: pay it          (ADR-0020)
    complete the message

If anything fails on the way:
    not the last delivery -> wait a moment (with jitter), abandon: the queue offers it again
    the last delivery     -> the dispute goes to a person, the message to the dead-letter queue

The work is safe to repeat (idempotent): a status only changes from the status it is expected to
be in, so a message delivered twice cannot finish a dispute twice.

Paying is a separate step that works from the SAVED approval, never from a new run of the graph.
If a worker dies after the decision was saved, the next delivery finds a finished dispute that is
still `refund_approved` and only pays it: the model is not asked again, so it cannot change its
mind about money that may already have moved. The idempotency key is `dispute:<dispute id>`, so
the ledger pays at most once however many times this step runs.
"""

import asyncio
import logging
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from langgraph.errors import GraphRecursionError

from services.supervisor.clients import RefundRefused, Specialists
from services.supervisor.graph import RECURSION_LIMIT, TriageContext, TriageState
from services.supervisor.messages import (
    INVESTIGATING,
    NEEDS_PERSON,
    customer_message,
    paid_message,
    payment_needs_person_message,
)
from services.supervisor.queue import MAX_DELIVERIES, Delivery, DisputeQueue
from services.supervisor.store import DisputeRecord, DisputeStore
from shared.auth import CallerIdentity
from shared.delegation import DelegationSettings, TokenSigner, issue_delegated_token
from shared.refund_policy import RefundPolicyConfig
from shared.schemas import (
    ApprovalRoute,
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
        # The customer was verified at submission. The agents get our own token for this
        # customer and this transaction; no customer login token exists here at all.
        token = await issue_delegated_token(
            self.signer, self.delegation, user_id=record.user_id,
            transaction_id=dispute.transaction_id, dispute_id=str(record.dispute_id),
        )
        identity = CallerIdentity(
            user_id=record.user_id, token_id=f"dispute-{record.dispute_id}",
            expires_at=datetime.now(UTC) + timedelta(seconds=self.delegation.lifetime_seconds),
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

    async def pay(self, record: DisputeRecord) -> None:
        """Pay the refund the saved result approved, and record the ledger's answer.

        Paid (or already paid)  -> refund_paid, with the ledger's refund ID
        A definite no (422 ...) -> a person: retrying would get the same answer
        No answer (network, 5xx) -> raise: the queue retries, and the same key cannot pay twice
        """
        result = TriageResult.model_validate(record.result)
        approval = result.approval
        if approval is None or approval.route != ApprovalRoute.AUTO_APPROVED or approval.approved_amount is None:
            raise RuntimeError(f"dispute {record.dispute_id} is refund_approved without an automatic approval")
        key = f"dispute:{record.dispute_id}"
        try:
            payment = await self.specialists.pay_refund(record.user_id, record.transaction_id,
                                                        approval.approved_amount, key)
        except RefundRefused as refused:
            log.warning("the ledger refused the refund for dispute %s: %s", record.dispute_id, refused)
            reason = f"the ledger refused the approved refund: {refused}"
            settled = result.model_copy(update={
                "status": DisputeStatus.PENDING_HUMAN_APPROVAL,
                "escalation_reason": reason,
                "customer_message": payment_needs_person_message(result.ledger),
                "steps": [*result.steps, f"pay: refused by the ledger ({refused.code}) -> a person"],
            })
            await self.store.settle(record.dispute_id, expected=DisputeStatus.REFUND_APPROVED, result=settled,
                                    note=f"refund refused by the ledger: {refused.code}")
            return
        settled = result.model_copy(update={
            "status": DisputeStatus.REFUND_PAID,
            "payment": payment,
            "customer_message": paid_message(result.ledger, payment),
            "steps": [*result.steps, f"pay: {payment.amount} {payment.currency} paid as {payment.refund_id}"],
        })
        await self.store.settle(record.dispute_id, expected=DisputeStatus.REFUND_APPROVED, result=settled,
                                note=f"refund paid: {payment.refund_id}")

    async def handle(self, delivery: Delivery) -> None:
        """Process one delivery. Never raises: every outcome ends in complete, abandon, or dead-letter."""
        dispute_id = delivery.dispute_id
        try:
            record = await self.store.load(dispute_id)
            if (record is not None and record.execution_status == ExecutionStatus.FINISHED
                    and record.business_status == DisputeStatus.REFUND_APPROVED):
                # The decision was saved but the payment did not complete (the worker died, or the
                # ledger did not answer). Resume from the saved approval: no new run of the graph.
                await self.pay(record)
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
                await self.pay(await self.store.load(dispute_id))
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
                    # Approved, but the ledger never answered. Whether it paid is unknown, so a
                    # person checks the ledger; the dispute is not left looking approved forever.
                    await self._payment_unknown(record, delivery)
                else:
                    await self.store.fail(delivery.dispute_id, NEEDS_PERSON,
                                          f"the run failed on delivery {delivery.delivery_count} of {MAX_DELIVERIES}")
                await self.queue.dead_letter(delivery, "the run failed on its last delivery")
            else:
                await asyncio.sleep(retry_delay(self.retry_delay_seconds))
                await self.queue.abandon(delivery)

    async def _payment_unknown(self, record: DisputeRecord, delivery: Delivery) -> None:
        result = TriageResult.model_validate(record.result)
        reason = (f"the ledger did not answer on delivery {delivery.delivery_count} of {MAX_DELIVERIES}; "
                  f"check the ledger for idempotency key dispute:{record.dispute_id}")
        settled = result.model_copy(update={
            "status": DisputeStatus.PENDING_HUMAN_APPROVAL,
            "escalation_reason": reason,
            "customer_message": payment_needs_person_message(result.ledger),
            "steps": [*result.steps, "pay: no answer from the ledger -> a person"],
        })
        await self.store.settle(record.dispute_id, expected=DisputeStatus.REFUND_APPROVED, result=settled,
                                note="payment outcome unknown: the ledger did not answer")

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
