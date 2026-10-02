"""What the customer is told about their dispute, chosen by code from the final state.

Rule: say only what has been established (docs/LEARNINGS.md, Part 2, entry D).
  - Facts come from the ledger figures (verified against the system of record) and from the
    deterministic refund policy. Nothing here is copied from text a model wrote.
  - Verbs match what has actually happened. Today a refund can be "recommended" or "approved";
    this system does not pay yet, so it never says "paid" or "sent". A case that needs a person
    is "marked for review": there is no review queue yet to have "sent" it to (Milestone 6).
  - If we do not know something, the message leaves it out rather than guessing.
"""

from shared.schemas import (
    ApprovalRoute,
    Decision,
    DisputeVerdict,
    LedgerReconciliation,
    RefundApproval,
    SettlementStatus,
)

NEEDS_PERSON = "We couldn't complete the review automatically, so the case is marked for review by a person."

# Before there is a result. Each is true only while the dispute is in that status.
RECEIVED = "We've received your dispute."
INVESTIGATING = "We're checking the records for this transfer."


def _records(ledger: LedgerReconciliation) -> str:
    """What the ledger shows, in the customer's terms."""
    debited, credited = f"{ledger.debited_amount} {ledger.currency}", f"{ledger.credited_amount} {ledger.currency}"
    return {
        SettlementStatus.FAILED: f"The transfer is marked as failed: {debited} was debited from your account "
                                 f"and {credited} reached the recipient.",
        SettlementStatus.SETTLED: f"The transfer is marked as completed: {credited} reached the recipient.",
        SettlementStatus.PENDING: f"The transfer is still in progress: {debited} was debited and it has not "
                                  "been credited yet.",
        SettlementStatus.REVERSED: "The transfer is marked as reversed: the amount was already returned.",
    }[ledger.settlement_status]


def _action(ledger: LedgerReconciliation | None, verdict: DisputeVerdict | None,
            approval: RefundApproval | None, escalated: bool) -> str:
    """What has been done about it. Each sentence is true only in the state that selects it."""
    if escalated or verdict is None:
        return NEEDS_PERSON
    if approval is not None:
        if approval.route == ApprovalRoute.AUTO_APPROVED:
            return (f"A refund of {approval.approved_amount} {ledger.currency} has been approved. "
                    "It has not been paid yet.")
        return (f"A refund of {ledger.discrepancy} {ledger.currency} has been recommended. "
                "It is marked for review by a person before it can be approved.")
    if verdict.decision == Decision.ESCALATE_FRAUD:
        return "The case is marked for review by our security team."
    if verdict.decision == Decision.NO_ACTION and ledger is not None:
        if ledger.settlement_status == SettlementStatus.PENDING:
            return "No refund decision is made while a transfer is in progress."
        if ledger.settlement_status == SettlementStatus.REVERSED:
            return "No further refund is due."
        return "No refund is due."
    return NEEDS_PERSON


def customer_message(
    *,
    ledger: LedgerReconciliation | None,
    verdict: DisputeVerdict | None,
    approval: RefundApproval | None,
    escalated: bool,
) -> str:
    action = _action(ledger, verdict, approval, escalated)
    return f"{_records(ledger)} {action}" if ledger is not None else action
