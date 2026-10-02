"""The customer is told only what has been established: facts from the ledger and the policy,
and verbs that match what has actually happened."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from services.supervisor.messages import NEEDS_PERSON, customer_message
from shared.schemas import DisputeVerdict, LedgerReconciliation, PolicyCheck, RefundApproval

TX = "TX-20261001000001"
MODEL_TEXT = "MODEL-WRITTEN TEXT THAT MUST NEVER REACH THE CUSTOMER MESSAGE"


def ledger(status="failed", debited="50000.00", credited="0.00"):
    return LedgerReconciliation(transaction_id=TX, settlement_status=status, debited_amount=debited,
                                credited_amount=credited, summary=MODEL_TEXT)


def verdict(decision="refund_recommended", amount="50000.00"):
    return DisputeVerdict(transaction_id=TX, decision=decision, explanation=MODEL_TEXT,
                          refund_amount=Decimal(amount) if decision == "refund_recommended" else None,
                          decided_at=datetime.now(UTC))


def approval(route="auto_approved", amount="50000.00"):
    passed = route == "auto_approved"
    return RefundApproval(dispute_id=uuid4(), transaction_id=TX, route=route,
                          approved_amount=Decimal(amount) if passed else None,
                          checks=[PolicyCheck(name="under_amount_limit", passed=passed, detail="x")],
                          policy_version="v", evaluated_at=datetime.now(UTC))


def message(**overrides):
    state = {"ledger": ledger(), "verdict": verdict(), "approval": None, "escalated": False} | overrides
    return customer_message(**state)


CASES = {
    "auto-approved refund": (
        {"approval": approval()},
        "The transfer is marked as failed: 50000.00 COP was debited from your account and 0.00 COP reached "
        "the recipient. A refund of 50000.00 COP has been approved. It has not been paid yet."),
    "refund needs a person": (
        {"ledger": ledger(debited="450000.00"), "verdict": verdict(amount="450000.00"), "approval": approval("human_required")},
        "The transfer is marked as failed: 450000.00 COP was debited from your account and 0.00 COP reached "
        "the recipient. A refund of 450000.00 COP has been recommended. It is marked for review by a person "
        "before it can be approved."),
    "settled": (
        {"ledger": ledger("settled", "80000.00", "80000.00"), "verdict": verdict("no_action")},
        "The transfer is marked as completed: 80000.00 COP reached the recipient. No refund is due."),
    "pending": (
        {"ledger": ledger("pending", "20000.00", "0.00"), "verdict": verdict("no_action")},
        "The transfer is still in progress: 20000.00 COP was debited and it has not been credited yet. "
        "No refund decision is made while a transfer is in progress."),
    "reversed": (
        {"ledger": ledger("reversed", "40000.00", "40000.00"), "verdict": verdict("no_action")},
        "The transfer is marked as reversed: the amount was already returned. No further refund is due."),
    "security review": (
        {"verdict": verdict("escalate_fraud")},
        "The transfer is marked as failed: 50000.00 COP was debited from your account and 0.00 COP reached "
        "the recipient. The case is marked for review by our security team."),
    "escalated with ledger evidence": (
        {"escalated": True},
        "The transfer is marked as failed: 50000.00 COP was debited from your account and 0.00 COP reached "
        f"the recipient. {NEEDS_PERSON}"),
    "escalated before any evidence": ({"ledger": None, "verdict": None, "escalated": True}, NEEDS_PERSON),
    "no verdict": ({"verdict": None}, None),
}


@pytest.mark.parametrize(("state", "expected"), CASES.values(), ids=CASES.keys())
def test_each_state_has_one_precise_message(state, expected):
    text = message(**state)
    if expected is not None:
        assert text == expected
    assert MODEL_TEXT not in text  # nothing a model wrote is repeated to the customer


@pytest.mark.parametrize("state", [case[0] for case in CASES.values()], ids=CASES.keys())
def test_no_message_claims_an_action_that_has_not_happened(state):
    text = message(**state).lower()
    for overclaim in ["has been paid", "has been sent", "we've sent", "we have sent", "refunded", "you will receive",
                      "has been received"]:
        assert overclaim not in text


def test_the_refund_amount_comes_from_the_policy_not_the_model():
    # The model recommended 90.000; the policy would never approve that, and the message cannot say it.
    text = message(verdict=verdict(amount="90000.00"), approval=approval("human_required"))
    assert "90000.00" not in text and "50000.00 COP has been recommended" in text


def test_missing_ledger_evidence_is_not_described():
    text = message(ledger=None, verdict=None, escalated=True)
    assert "transfer is" not in text  # we do not describe records we did not get
