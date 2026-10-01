"""Deterministic refund approval policy (ADR-0007). No LLM is involved in this module.

The LLM supervisor *recommends* a refund (DisputeVerdict). This module decides, from facts
reported by the systems of record (core banking ledger, risk engine, refund history), whether
the refund is auto-approved or goes to a human. Any failed check routes to a human.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from shared.schemas import (
    ApprovalRoute,
    Decision,
    DisputeVerdict,
    FraudAssessment,
    LedgerReconciliation,
    PolicyCheck,
    RefundApproval,
    RiskLevel,
    SettlementStatus,
)

POLICY_VERSION = "refund-policy/v1"


@dataclass(frozen=True)
class RefundPolicyConfig:
    """Live limits. Defaults are the ADR-0007 starting values; override via env (a ConfigMap in AKS)."""

    enabled: bool = True  # kill switch: false sends every refund to a human
    max_auto_amount: Decimal = Decimal("100000.00")  # per dispute, COP
    max_auto_refunds: int = 3  # per customer, per window
    max_auto_total: Decimal = Decimal("200000.00")  # per customer, per window, COP
    window_days: int = 30

    @classmethod
    def from_env(cls) -> "RefundPolicyConfig":
        """Read AUTO_REFUND_* variables. Invalid values raise at startup (fail fast, never guess)."""
        default = cls()
        enabled = os.environ.get("AUTO_REFUND_ENABLED", str(default.enabled)).strip().lower()
        if enabled not in {"true", "false"}:
            raise ValueError(f"AUTO_REFUND_ENABLED must be true or false, got {enabled!r}")
        return cls(
            enabled=enabled == "true",
            max_auto_amount=Decimal(os.environ.get("AUTO_REFUND_MAX_AMOUNT", default.max_auto_amount)),
            max_auto_refunds=int(os.environ.get("AUTO_REFUND_MAX_COUNT", default.max_auto_refunds)),
            max_auto_total=Decimal(os.environ.get("AUTO_REFUND_MAX_TOTAL", default.max_auto_total)),
            window_days=int(os.environ.get("AUTO_REFUND_WINDOW_DAYS", default.window_days)),
        )


@dataclass(frozen=True)
class CustomerRefundHistory:
    """Auto-refunds already granted to this customer within the window (from core banking)."""

    auto_refund_count: int
    auto_refund_total: Decimal


def evaluate(
    verdict: DisputeVerdict,
    ledger: LedgerReconciliation,
    fraud: FraudAssessment,
    history: CustomerRefundHistory,
    config: RefundPolicyConfig,
    now: datetime | None = None,
) -> RefundApproval:
    """Decide AUTO_APPROVED vs HUMAN_REQUIRED for a refund recommendation."""
    if verdict.decision != Decision.REFUND_RECOMMENDED:
        raise ValueError(f"refund policy only applies to {Decision.REFUND_RECOMMENDED}, got {verdict.decision}")

    # The amount always comes from the ledger (system of record), never from the LLM or customer.
    amount = ledger.discrepancy
    same_tx = verdict.transaction_id == ledger.transaction_id == fraud.transaction_id

    checks = [
        PolicyCheck(name="auto_refund_enabled", passed=config.enabled,
                    detail="kill switch on" if config.enabled else "kill switch off: all refunds need a human"),
        PolicyCheck(name="same_transaction", passed=same_tx,
                    detail="verdict, ledger, and fraud refer to the same transaction" if same_tx
                    else "verdict, ledger, and fraud refer to different transactions"),
        PolicyCheck(name="ledger_shows_failed_transfer", passed=ledger.settlement_status == SettlementStatus.FAILED,
                    detail=f"settlement_status={ledger.settlement_status}"),  # REVERSED = already refunded
        PolicyCheck(name="amount_matches_ledger", passed=verdict.refund_amount == amount,
                    detail=f"recommended={verdict.refund_amount} ledger_discrepancy={amount}"),
        PolicyCheck(name="fraud_risk_low", passed=fraud.risk_level == RiskLevel.LOW,
                    detail=f"risk_level={fraud.risk_level} score={fraud.risk_score}"),
        PolicyCheck(name="under_amount_limit", passed=Decimal(0) < amount <= config.max_auto_amount,
                    detail=f"{amount} <= {config.max_auto_amount}"),
        PolicyCheck(name="under_refund_count_limit",
                    passed=history.auto_refund_count + 1 <= config.max_auto_refunds,
                    detail=f"{history.auto_refund_count} + 1 <= {config.max_auto_refunds} in {config.window_days}d"),
        PolicyCheck(name="under_refund_total_limit",
                    passed=history.auto_refund_total + amount <= config.max_auto_total,
                    detail=f"{history.auto_refund_total} + {amount} <= {config.max_auto_total} in {config.window_days}d"),
    ]

    auto = all(check.passed for check in checks)
    return RefundApproval(
        dispute_id=verdict.dispute_id,
        transaction_id=verdict.transaction_id,
        route=ApprovalRoute.AUTO_APPROVED if auto else ApprovalRoute.HUMAN_REQUIRED,
        approved_amount=amount if auto else None,
        checks=checks,
        policy_version=POLICY_VERSION,
        evaluated_at=now or datetime.now(UTC),
    )
