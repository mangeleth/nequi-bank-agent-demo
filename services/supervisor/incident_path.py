"""The known-incident fast path (ADR-0022): decide a dispute without a model when a confirmed
incident already explains it.

    normal path    3 agents, about 8 model calls: investigate what happened
    this path      0 model calls: operations already confirmed what happened

It removes the INVESTIGATION, not the CONTROLS. The refund policy's `evaluate()` gets the same
four inputs as on the normal path; only where three of them come from changes:

    input             normal path                      this path
    ledger figures    Ledger Agent (a model)           Core Banking, read by code
    fraud risk        Fraud Agent (a model)            the risk engine's own score, read by code
    recommendation    the supervisor model             code, citing the incident
    refund history    code                             code (unchanged)

So the kill switch, the amount limit, "the amount equals what the ledger shows owed", and the
30-day limits all still apply, and anything the policy does not approve goes to a person.
"""

from datetime import UTC, datetime
from uuid import UUID

from services.supervisor.clients import Specialists, SpecialistUnavailable
from services.supervisor.graph import TriageState
from services.supervisor.messages import incident_sentence
from shared.auth import CallerIdentity
from shared.refund_policy import RefundPolicyConfig, evaluate
from shared.schemas import Decision, DisputeRequest, DisputeVerdict, KnownIncident, SettlementStatus


async def decide_known_incident(
    specialists: Specialists, caller: CallerIdentity, dispute_id: UUID, dispute: DisputeRequest,
    policy: RefundPolicyConfig,
) -> tuple[KnownIncident, TriageState] | None:
    """The final triage state for a dispute a confirmed incident covers, or None if none covers it
    (the caller then runs the agents as usual). Raises SpecialistUnavailable if Core Systems does
    not answer: the delivery is retried, rather than spending model calls during an outage."""
    incident = await specialists.known_incident(caller, dispute.transaction_id)
    if incident is None:
        return None

    ledger = await specialists.ledger_record(caller, dispute.transaction_id)
    steps = [f"incident: {incident.incident_id} covers {dispute.transaction_id} -> decided without a model"]
    owed = ledger.discrepancy

    if ledger.settlement_status != SettlementStatus.FAILED or owed <= 0:
        # Covered, but nothing is owed (for example the batch job already refunded it).
        verdict = DisputeVerdict(
            dispute_id=dispute_id, transaction_id=dispute.transaction_id, decision=Decision.NO_ACTION,
            explanation=(f"Covered by incident {incident.incident_id}; the ledger shows "
                         f"{ledger.settlement_status.value} with nothing owed."),
            decided_at=datetime.now(UTC))
        return incident, {"ledger": ledger, "verdict": verdict, "steps": [*steps, "verdict: no_action (nothing owed)"]}

    fraud = await specialists.risk_engine(caller, dispute.transaction_id)
    verdict = DisputeVerdict(
        dispute_id=dispute_id, transaction_id=dispute.transaction_id, decision=Decision.REFUND_RECOMMENDED,
        refund_amount=owed,  # the ledger's figure; the customer's claimed amount is not used
        explanation=(f"Covered by confirmed incident {incident.incident_id} ({incident.title}); "
                     f"the ledger shows {owed} {ledger.currency} debited and never credited."),
        decided_at=datetime.now(UTC))
    try:
        history = await specialists.refund_history(caller, policy.window_days)
    except SpecialistUnavailable:
        # Fail closed, as on the normal path: never assume an empty refund history.
        reason = "the customer's refund history was unavailable, so the refund policy could not be applied"
        return incident, {"ledger": ledger, "fraud": fraud, "verdict": verdict, "escalation_reason": reason,
                          "steps": [*steps, "verdict: refund_recommended", f"escalate: {reason}"]}

    approval = evaluate(verdict, ledger, fraud, history, policy)  # the same policy as every dispute
    failed = [check.name for check in approval.checks if not check.passed]
    return incident, {
        "ledger": ledger, "fraud": fraud, "verdict": verdict, "approval": approval,
        "steps": [*steps, f"verdict: refund_recommended {owed} (from the ledger)",
                  f"policy: {approval.route.value}" + (f" (failed: {', '.join(failed)})" if failed else "")],
    }


def with_incident(message: str, incident: KnownIncident) -> str:
    return f"{incident_sentence(incident)} {message}"
