"""Paying approved refunds, at a controlled pace (ADR-0020, ADR-0021).

The triage worker decides; it does not pay. When a refund is approved it saves the decision and
puts the dispute ID on the `refunds` queue. The refund payer takes from that queue and pays:

    triage-worker   decide -> save refund_approved -> send the ID to "refunds"
    refund-payer    receive at most N per second -> pay -> refund_paid (or a person)

Why a separate queue and process:
  - Pace. A burst of 500 approvals still reaches the bank's ledger at N payments per second; the
    rest wait in the queue, where they are safe.
  - Pause. Operations can stop payments (a ledger incident) while triage keeps approving.
  - Least privilege. The payer has no model access and cannot sign tokens; the process that runs
    the model never calls the endpoint that moves money.
  - Patience. A ledger outage can last a while, so a refund gets more deliveries than a triage.

Paying works from the SAVED approval. The idempotency key is `dispute:<dispute id>`, so the ledger
pays at most once however many times a message is delivered.
"""

import asyncio
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field

from services.supervisor.clients import RefundRefused, Specialists
from services.supervisor.messages import paid_message, payment_needs_person_message
from services.supervisor.queue import Delivery, DisputeQueue
from services.supervisor.store import DisputeRecord, DisputeStore
from services.supervisor.triage import retry_delay
from shared.schemas import ApprovalRoute, DisputeStatus, ExecutionStatus, TriageResult

log = logging.getLogger("payments")

REFUND_MAX_DELIVERIES = 5  # must equal the refunds queue's --max-delivery-count
REFUND_RETRY_DELAY_SECONDS = 10.0  # base wait before a payment with no answer is tried again
MAX_CONCURRENT_PAYMENTS = 4  # in flight at once; the pace limits how many START per second


def awaiting_payment(record: DisputeRecord | None) -> bool:
    """The decision is saved and approved, and no payment outcome has been recorded yet."""
    return (record is not None and record.execution_status == ExecutionStatus.FINISHED
            and record.business_status == DisputeStatus.REFUND_APPROVED)


async def pay_approved(store: DisputeStore, specialists: Specialists, record: DisputeRecord) -> None:
    """Pay the refund the saved result approved, and record the ledger's answer.

    Paid (or already paid)   -> refund_paid, with the ledger's refund ID
    A definite no (422 ...)  -> a person: retrying would get the same answer
    No answer (network, 5xx) -> raise: the caller retries, and the same key cannot pay twice
    """
    result = TriageResult.model_validate(record.result)
    approval = result.approval
    if approval is None or approval.route != ApprovalRoute.AUTO_APPROVED or approval.approved_amount is None:
        raise RuntimeError(f"dispute {record.dispute_id} is refund_approved without an automatic approval")
    key = f"dispute:{record.dispute_id}"
    try:
        payment = await specialists.pay_refund(record.user_id, record.transaction_id, approval.approved_amount, key)
    except RefundRefused as refused:
        log.warning("the ledger refused the refund for dispute %s: %s", record.dispute_id, refused)
        await to_a_person(store, record, reason=f"the ledger refused the approved refund: {refused}",
                          step=f"pay: refused by the ledger ({refused.code}) -> a person",
                          note=f"refund refused by the ledger: {refused.code}")
        return
    settled = result.model_copy(update={
        "status": DisputeStatus.REFUND_PAID,
        "payment": payment,
        "customer_message": _with_incident(result, paid_message(result.ledger, payment)),
        "steps": [*result.steps, f"pay: {payment.amount} {payment.currency} paid as {payment.refund_id}"],
    })
    await store.settle(record.dispute_id, expected=DisputeStatus.REFUND_APPROVED, result=settled,
                       note=f"refund paid: {payment.refund_id}")


async def to_a_person(store: DisputeStore, record: DisputeRecord, *, reason: str, step: str, note: str) -> None:
    """An approved refund that will not be paid automatically: refund_approved -> a person."""
    result = TriageResult.model_validate(record.result)
    settled = result.model_copy(update={
        "status": DisputeStatus.PENDING_HUMAN_APPROVAL,
        "escalation_reason": reason,
        "customer_message": _with_incident(result, payment_needs_person_message(result.ledger)),
        "steps": [*result.steps, step],
    })
    await store.settle(record.dispute_id, expected=DisputeStatus.REFUND_APPROVED, result=settled, note=note)


def _with_incident(result: TriageResult, message: str) -> str:
    if result.incident is None:
        return message
    from services.supervisor.incident_path import with_incident

    return with_incident(message, result.incident)


class Pace:
    """At most `per_second` payments START per second, however many are waiting.

    One payer replica runs in the cluster, so this is also the rate the ledger sees. With more
    replicas the limit would have to be shared (ADR-0021, production delta).
    """

    def __init__(self, per_second: float) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be positive")
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


@dataclass
class RefundPayer:
    store: DisputeStore
    queue: DisputeQueue  # the refunds queue
    specialists: Specialists
    per_second: float = 2.0
    paused: bool = False
    retry_delay_seconds: float = REFUND_RETRY_DELAY_SECONDS
    shutdown_grace_seconds: float = 30.0
    pace: Pace = field(init=False)

    def __post_init__(self) -> None:
        self.pace = Pace(self.per_second)

    @classmethod
    def settings_from_env(cls) -> dict:
        return {
            "per_second": float(os.environ.get("REFUND_PAYMENTS_PER_SECOND", "2")),
            "paused": os.environ.get("REFUND_PAYMENTS_PAUSED", "false").strip().lower() == "true",
        }

    async def handle(self, delivery: Delivery) -> None:
        """Pay one refund. Never raises: every outcome ends in complete, abandon, or dead-letter."""
        try:
            record = await self.store.load(delivery.dispute_id)
            if not awaiting_payment(record):
                await self.queue.complete(delivery)  # already paid, or with a person: a duplicate message
                return
            await pay_approved(self.store, self.specialists, record)
            await self.queue.complete(delivery)
        except Exception as exc:
            log.warning("payment delivery %d/%d of dispute %s failed: %s", delivery.delivery_count,
                        delivery.max_deliveries, delivery.dispute_id, exc)
            with suppress(Exception):  # if even this fails, the lock expires and the queue redelivers
                if delivery.is_last:
                    record = await self.store.load(delivery.dispute_id)
                    if awaiting_payment(record):
                        await to_a_person(
                            self.store, record,
                            reason=(f"the ledger did not answer on delivery {delivery.delivery_count} of "
                                    f"{delivery.max_deliveries}; check the ledger for idempotency key "
                                    f"dispute:{record.dispute_id}"),
                            step="pay: no answer from the ledger -> a person",
                            note="payment outcome unknown: the ledger did not answer")
                    await self.queue.dead_letter(delivery, "payment failed on its last delivery")
                else:
                    await asyncio.sleep(retry_delay(self.retry_delay_seconds))
                    await self.queue.abandon(delivery)

    async def run_forever(self) -> None:
        if self.paused:
            # Payments stopped by operations. Approved refunds wait in the queue, safely, until
            # REFUND_PAYMENTS_PAUSED is set back to "false".
            log.warning("refund payments are PAUSED: approved refunds wait in the queue")
            await asyncio.Event().wait()
        slots = asyncio.Semaphore(MAX_CONCURRENT_PAYMENTS)
        running: set[asyncio.Task] = set()

        async def one(delivery: Delivery) -> None:
            try:
                await self.handle(delivery)
            finally:
                slots.release()

        deliveries = self.queue.receive().__aiter__()
        try:
            while True:
                await slots.acquire()
                await self.pace.wait()  # the ledger never sees more than per_second new payments
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
                _, unfinished = await asyncio.wait(running, timeout=self.shutdown_grace_seconds)
                for task in unfinished:
                    task.cancel()
