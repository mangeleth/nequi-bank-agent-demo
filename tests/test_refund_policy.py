from datetime import UTC, datetime
from decimal import Decimal

import pytest

from shared.refund_policy import CustomerRefundHistory, RefundPolicyConfig, evaluate
from shared.schemas import (
    ApprovalRoute,
    Decision,
    DisputeStatus,
    DisputeVerdict,
    FraudAssessment,
    LedgerReconciliation,
    RiskLevel,
    SettlementStatus,
)

TX = "TX-20261001000123"
CONFIG = RefundPolicyConfig()
CLEAN_HISTORY = CustomerRefundHistory(auto_refund_count=0, auto_refund_total=Decimal("0"))


def verdict(amount="50000.00", tx=TX, decision=Decision.REFUND_RECOMMENDED):
    return DisputeVerdict(
        transaction_id=tx, decision=decision, explanation="Debited, never credited.",
        refund_amount=Decimal(amount) if decision == Decision.REFUND_RECOMMENDED else None,
        decided_at=datetime.now(UTC),
    )


def ledger(debited="50000.00", credited="0", status=SettlementStatus.FAILED, tx=TX):
    return LedgerReconciliation(
        transaction_id=tx, settlement_status=status,
        debited_amount=Decimal(debited), credited_amount=Decimal(credited),
    )


def fraud(score=0.1, level=RiskLevel.LOW, tx=TX):
    return FraudAssessment(transaction_id=tx, risk_score=score, risk_level=level, rationale="ok")


def run(v=None, l=None, f=None, h=CLEAN_HISTORY, c=CONFIG):
    return evaluate(v or verdict(), l or ledger(), f or fraud(), h, c)


def failed(approval):
    return {check.name for check in approval.checks if not check.passed}


def test_small_clear_case_is_auto_approved_with_ledger_amount():
    approval = run()
    assert approval.route == ApprovalRoute.AUTO_APPROVED
    assert approval.approved_amount == Decimal("50000.00")
    assert approval.status == DisputeStatus.RESOLVED
    assert failed(approval) == set()


@pytest.mark.parametrize(
    ("kwargs", "expected_failure"),
    [
        ({"c": RefundPolicyConfig(enabled=False)}, "auto_refund_enabled"),
        ({"v": verdict(amount="150000.00"), "l": ledger(debited="150000.00")}, "under_amount_limit"),
        ({"f": fraud(score=0.5, level=RiskLevel.MEDIUM)}, "fraud_risk_low"),
        ({"l": ledger(status=SettlementStatus.PENDING)}, "ledger_shows_failed_transfer"),
        ({"l": ledger(status=SettlementStatus.REVERSED)}, "ledger_shows_failed_transfer"),  # already refunded
        ({"l": ledger(tx="TX-99999999")}, "same_transaction"),
    ],
)
def test_any_failed_check_routes_to_human(kwargs, expected_failure):
    approval = run(**kwargs)
    assert approval.route == ApprovalRoute.HUMAN_REQUIRED
    assert approval.approved_amount is None
    assert approval.status == DisputeStatus.PENDING_HUMAN_APPROVAL
    assert expected_failure in failed(approval)


def test_llm_cannot_inflate_the_refund():
    # Prompt injection convinced the LLM to recommend more than the ledger shows.
    approval = run(v=verdict(amount="90000.00"), l=ledger(debited="50000.00"))
    assert approval.route == ApprovalRoute.HUMAN_REQUIRED
    assert "amount_matches_ledger" in failed(approval)


def test_many_small_disputes_hit_the_count_limit():
    approval = run(h=CustomerRefundHistory(auto_refund_count=3, auto_refund_total=Decimal("30000")))
    assert approval.route == ApprovalRoute.HUMAN_REQUIRED
    assert failed(approval) == {"under_refund_count_limit"}


def test_window_total_limit():
    approval = run(h=CustomerRefundHistory(auto_refund_count=1, auto_refund_total=Decimal("160000.00")))
    assert failed(approval) == {"under_refund_total_limit"}  # 160.000 + 50.000 > 200.000


def test_exactly_at_limits_is_allowed():
    approval = run(
        v=verdict(amount="100000.00"), l=ledger(debited="100000.00"),
        h=CustomerRefundHistory(auto_refund_count=2, auto_refund_total=Decimal("100000.00")),
    )
    assert approval.route == ApprovalRoute.AUTO_APPROVED


def test_policy_refuses_non_refund_verdicts():
    with pytest.raises(ValueError, match="only applies"):
        run(v=verdict(decision=Decision.NO_ACTION))


def test_every_check_is_recorded_for_audit():
    approval = run(f=fraud(score=0.9, level=RiskLevel.HIGH))
    assert len(approval.checks) == 8
    assert approval.policy_version == "refund-policy/v1"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("AUTO_REFUND_ENABLED", "false")
    monkeypatch.setenv("AUTO_REFUND_MAX_AMOUNT", "150000.00")
    config = RefundPolicyConfig.from_env()
    assert config.enabled is False
    assert config.max_auto_amount == Decimal("150000.00")
    assert config.max_auto_refunds == 3  # default kept


def test_invalid_kill_switch_fails_fast(monkeypatch):
    monkeypatch.setenv("AUTO_REFUND_ENABLED", "yes")
    with pytest.raises(ValueError, match="true or false"):
        RefundPolicyConfig.from_env()
