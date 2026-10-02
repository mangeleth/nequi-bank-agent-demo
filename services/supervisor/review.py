"""A person's decision on a dispute the system sent to them (ADR-0027).

    approve  the amount is what the LEDGER shows owed, read now by code, never typed by the
             reviewer. The dispute becomes refund_approved (route human_approved, with the
             reviewer's name) and goes on the refunds queue: the same payer pays it, exactly once,
             with the same idempotency key as any refund (ADR-0020, ADR-0021).
    reject   the dispute becomes rejected. A note is required, for the audit trail.

Only a dispute still waiting for a person can be decided, and only once: the store refuses a
second decision. Every decision is a line in the audit trail: who, what, and why.
"""

import logging
from datetime import UTC, datetime, timedelta

from services.supervisor.clients import Specialists
from services.supervisor.messages import human_approved_message, human_rejected_message
from services.supervisor.payments import to_a_person
from services.supervisor.queue import DisputeQueue
from services.supervisor.store import DisputeRecord, DisputeStore
from shared.auth import CallerIdentity, ReviewerIdentity
from shared.schemas import (
    ApprovalRoute,
    DisputeStatus,
    PolicyCheck,
    RefundApproval,
    ReviewDecision,
    SettlementStatus,
    TriageResult,
)

log = logging.getLogger("review")


class ReviewRefused(Exception):
    """The decision cannot be made: `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _current(record: DisputeRecord) -> TriageResult:
    """The dispute's result so far, or a minimal one if its run failed before producing any."""
    if record.result:
        return TriageResult.model_validate(record.result)
    return TriageResult(dispute_id=record.dispute_id, transaction_id=record.transaction_id,
                        status=record.business_status, customer_message=record.customer_message,
                        escalation_reason="the run did not finish", steps=[])


async def decide(*, store: DisputeStore, specialists: Specialists, refunds: DisputeQueue, record: DisputeRecord,
                 reviewer: ReviewerIdentity, decision: str, note: str) -> None:
    if record.business_status != DisputeStatus.PENDING_HUMAN_APPROVAL:
        raise ReviewRefused(409, f"this dispute is not waiting for a person (it is {record.business_status.value})")
    result = _current(record)
    now = datetime.now(UTC)

    if decision == "reject":
        review = ReviewDecision(decision="reject", reviewer_id=reviewer.reviewer_id, note=note, decided_at=now)
        decided = result.model_copy(update={
            "status": DisputeStatus.REJECTED, "review": review,
            "customer_message": human_rejected_message(result.ledger),
            "steps": [*result.steps, f"review: rejected by {reviewer.reviewer_id}"],
        })
        if not await store.decide(record.dispute_id, decided, f"rejected by {reviewer.reviewer_id}: {note}"):
            raise ReviewRefused(409, "this dispute was already decided")
        return

    # Approve: the amount comes from the ledger, now. Not from the model, the customer, or the reviewer.
    customer = CallerIdentity(user_id=record.user_id, token_id=f"review-{reviewer.token_id}",
                              expires_at=now + timedelta(minutes=5))
    ledger = await specialists.ledger_record(customer, record.transaction_id)
    owed = ledger.discrepancy
    if ledger.settlement_status != SettlementStatus.FAILED or owed <= 0:
        raise ReviewRefused(409, f"the ledger shows nothing owed ({ledger.settlement_status.value}, "
                                 f"{owed} {ledger.currency}): reject it instead")
    checks = list(result.approval.checks) if result.approval else []
    checks.append(PolicyCheck(name="approved_by_a_person", passed=True,
                              detail=f"{reviewer.reviewer_id}: {note}"[:200]))
    approval = RefundApproval(
        dispute_id=record.dispute_id, transaction_id=record.transaction_id, route=ApprovalRoute.HUMAN_APPROVED,
        approved_amount=owed, approved_by=reviewer.reviewer_id, checks=checks,
        policy_version=result.approval.policy_version if result.approval else "human-review", evaluated_at=now,
    )
    review = ReviewDecision(decision="approve", reviewer_id=reviewer.reviewer_id, note=note, decided_at=now,
                            amount=owed)
    decided = result.model_copy(update={
        "status": DisputeStatus.REFUND_APPROVED, "approval": approval, "ledger": ledger, "review": review,
        "escalation_reason": None, "customer_message": human_approved_message(ledger, owed),
        "steps": [*result.steps, f"review: approved {owed} {ledger.currency} by {reviewer.reviewer_id}"],
    })
    if not await store.decide(record.dispute_id, decided, f"approved {owed} by {reviewer.reviewer_id}: {note}"):
        raise ReviewRefused(409, "this dispute was already decided")
    try:
        await refunds.send(record.dispute_id)  # the refund payer pays it, at its pace
    except Exception:
        # Not queued, so nobody would pay it: back to the review queue, and say why.
        log.exception("could not queue the approved refund for dispute %s", record.dispute_id)
        approved = await store.load(record.dispute_id)
        await to_a_person(store, approved, reason="the approved refund could not be queued for payment; approve it again",
                          step="pay: could not be queued -> a person", note="approved refund could not be queued")
        raise ReviewRefused(503, "the refund could not be queued for payment; the dispute is back in the review queue")
