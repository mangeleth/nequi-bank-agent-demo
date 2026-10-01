"""Data contracts shared by the UI, supervisor, and worker agents (Pydantic v2).

Design rules (ADR-0006):
  - Money is Decimal, never float.
  - Every model rejects unknown fields, so an LLM cannot invent `refund_approved: true`.
  - No contract accepts a caller-supplied `user_id`: identity comes from the verified JWT (Step 3).
  - The LLM can only *recommend* a refund (DisputeVerdict). Whether it runs automatically or
    waits for a human is decided by deterministic code (RefundApproval, ADR-0007).
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# --- Building blocks --------------------------------------------------------------------------



def _reject_float(value: object) -> object:
    # A float has already lost precision before we see it (0.1 is really 0.1000000000000000055...).
    # Accept Decimal, int, or numeric strings like "150000.00" (how money should travel in JSON).
    if isinstance(value, float | bool):
        raise ValueError("money must be a Decimal or a numeric string, never a float")
    return value


Amount = Annotated[Decimal, BeforeValidator(_reject_float), Field(ge=0, max_digits=15, decimal_places=2)]
"""A non-negative amount with at most 2 decimals; floats are rejected."""

Money = Annotated[Amount, Field(gt=0)]
"""A strictly positive amount, e.g. what the customer claims or what we refund."""

Currency = Literal["COP"]

TransactionId = Annotated[str, Field(pattern=r"^TX-[0-9]{8,20}$", examples=["TX-20261001000123"])]
"""Nequi-style transaction reference. The pattern blocks free text (and prompt injection) in IDs."""

Score = Annotated[float, Field(ge=0.0, le=1.0)]
"""Probabilities and risk scores: float is fine here, these are not money."""


class Contract(BaseModel):
    """Base for every contract: unknown fields are errors, instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# --- Enums ------------------------------------------------------------------------------------


class DisputeReason(StrEnum):
    FAILED_TRANSFER = "failed_transfer"  # money left my account but never arrived
    DUPLICATE_CHARGE = "duplicate_charge"
    UNRECOGNIZED_TRANSACTION = "unrecognized_transaction"


class DisputeStatus(StrEnum):
    """Lifecycle: RECEIVED -> INVESTIGATING -> (PENDING_HUMAN_APPROVAL ->) RESOLVED | REJECTED."""

    RECEIVED = "received"
    INVESTIGATING = "investigating"
    PENDING_HUMAN_APPROVAL = "pending_human_approval"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SettlementStatus(StrEnum):
    SETTLED = "settled"  # debited and credited
    PENDING = "pending"  # in flight
    FAILED = "failed"  # debited, credit failed: the classic dispute case
    REVERSED = "reversed"  # already refunded


class Decision(StrEnum):
    REFUND_RECOMMENDED = "refund_recommended"  # needs human approval, never auto-executed
    NO_ACTION = "no_action"  # e.g. transfer is still pending, or already reversed
    ESCALATE_FRAUD = "escalate_fraud"  # route to fraud operations team


# --- Contracts --------------------------------------------------------------------------------


class DisputeRequest(Contract):
    """What the customer submits from the app. Deliberately has NO user_id field."""

    transaction_id: TransactionId
    reason: DisputeReason
    claimed_amount: Money
    currency: Currency = "COP"
    description: str = Field(default="", max_length=500)  # free text: treat as untrusted LLM input


class FraudAssessment(Contract):
    """Fraud Agent output."""

    transaction_id: TransactionId
    risk_score: Score
    risk_level: RiskLevel
    signals: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=10)
    rationale: str = Field(max_length=1000)

    @model_validator(mode="after")
    def level_matches_score(self) -> "FraudAssessment":
        # The LLM picks both; make sure they cannot contradict each other.
        expected = RiskLevel.LOW if self.risk_score < 0.4 else RiskLevel.MEDIUM if self.risk_score < 0.7 else RiskLevel.HIGH
        if self.risk_level != expected:
            raise ValueError(f"risk_level {self.risk_level} contradicts risk_score {self.risk_score} (expected {expected})")
        return self


class LedgerReconciliation(Contract):
    """Ledger Agent output: what the core banking system says actually happened to the money."""

    transaction_id: TransactionId
    settlement_status: SettlementStatus
    debited_amount: Amount
    credited_amount: Amount
    currency: Currency = "COP"

    @property
    def discrepancy(self) -> Decimal:
        """Money that left the sender but did not reach the receiver. Exact, because Decimal."""
        return self.debited_amount - self.credited_amount


class DisputeVerdict(Contract):
    """Supervisor's (LLM) final answer: a recommendation only. It cannot approve or execute anything."""

    dispute_id: UUID = Field(default_factory=uuid4)
    transaction_id: TransactionId
    decision: Decision
    refund_amount: Money | None = None
    explanation: str = Field(max_length=1000)
    decided_at: datetime

    @model_validator(mode="after")
    def amount_only_with_refund(self) -> "DisputeVerdict":
        if self.decision == Decision.REFUND_RECOMMENDED and self.refund_amount is None:
            raise ValueError("refund_recommended requires refund_amount")
        if self.decision != Decision.REFUND_RECOMMENDED and self.refund_amount is not None:
            raise ValueError(f"refund_amount is only allowed with {Decision.REFUND_RECOMMENDED}")
        return self


# --- Deterministic approval (no LLM) ---------------------------------------------------------


class ApprovalRoute(StrEnum):
    AUTO_APPROVED = "auto_approved"  # every policy check passed: refund may execute
    HUMAN_REQUIRED = "human_required"  # at least one check failed: goes to the review queue


class PolicyCheck(Contract):
    """One rule of the refund policy, recorded for the audit trail."""

    name: str
    passed: bool
    detail: str


class RefundApproval(Contract):
    """Output of shared.refund_policy.evaluate(): who approves the refund, and why."""

    dispute_id: UUID
    transaction_id: TransactionId
    route: ApprovalRoute
    approved_amount: Money | None = None  # taken from the ledger, never from the LLM
    checks: list[PolicyCheck]
    policy_version: str
    evaluated_at: datetime

    @model_validator(mode="after")
    def auto_only_if_every_check_passed(self) -> "RefundApproval":
        failed = [c.name for c in self.checks if not c.passed]
        if self.route == ApprovalRoute.AUTO_APPROVED:
            if failed or not self.checks:
                raise ValueError(f"auto_approved requires every check to pass (failed: {failed})")
            if self.approved_amount is None:
                raise ValueError("auto_approved requires approved_amount")
        elif self.approved_amount is not None:
            raise ValueError("approved_amount is only set when auto_approved; a human sets it otherwise")
        return self

    @property
    def status(self) -> DisputeStatus:
        return DisputeStatus.RESOLVED if self.route == ApprovalRoute.AUTO_APPROVED else DisputeStatus.PENDING_HUMAN_APPROVAL
