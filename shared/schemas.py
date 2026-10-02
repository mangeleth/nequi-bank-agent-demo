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
    """Business status: where the customer's dispute stands. Each value is a fact that has
    happened, so there is no "resolved": an approved refund is not a paid refund.

        RECEIVED -> INVESTIGATING -> CLOSED_NO_REFUND
                                  -> REFUND_APPROVED -> REFUND_PAID   (the ledger confirmed it)
                                                     -> PENDING_HUMAN_APPROVAL (the ledger refused it)
                                  -> PENDING_HUMAN_APPROVAL -> REFUND_APPROVED | REJECTED
    """

    RECEIVED = "received"
    INVESTIGATING = "investigating"
    PENDING_HUMAN_APPROVAL = "pending_human_approval"  # a person must decide
    REFUND_APPROVED = "refund_approved"  # approved, not yet paid
    REFUND_PAID = "refund_paid"  # the ledger confirmed the payment
    CLOSED_NO_REFUND = "closed_no_refund"  # investigated; no refund is due
    REJECTED = "rejected"  # a person refused the refund


class ExecutionStatus(StrEnum):
    """Execution status: what happened to the run of the graph. For engineers and operations.

    Separate from the business status: a run can finish while the dispute still waits for a
    person, and a run can fail without the customer's dispute being lost.
    """

    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


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
    summary: str = Field(min_length=1, max_length=1000)  # plain-language explanation for a reviewer

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
    HUMAN_APPROVED = "human_approved"  # a reviewer approved it (ADR-0027): refund may execute


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
    approved_amount: Money | None = None  # taken from the ledger, never from the LLM or a person
    approved_by: str | None = None  # the reviewer, for human_approved
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
        elif self.route == ApprovalRoute.HUMAN_APPROVED:
            if self.approved_amount is None or not self.approved_by:
                raise ValueError("human_approved requires approved_amount and approved_by")
        elif self.approved_amount is not None:
            raise ValueError("approved_amount is only set when a refund is approved")
        return self

    @property
    def status(self) -> DisputeStatus:
        if self.route in (ApprovalRoute.AUTO_APPROVED, ApprovalRoute.HUMAN_APPROVED):
            return DisputeStatus.REFUND_APPROVED  # approved is not paid
        return DisputeStatus.PENDING_HUMAN_APPROVAL


class KnownIncident(Contract):
    """A confirmed platform incident that covers the disputed transaction (ADR-0022). When one
    exists, the dispute is decided by code: there is nothing left to investigate."""

    incident_id: Annotated[str, Field(pattern=r"^INC-[0-9]{8}-[0-9]{2}$")]
    title: str = Field(min_length=1, max_length=200)
    confirmed_by: str = Field(min_length=1, max_length=100)


class ReviewDecision(Contract):
    """A person's decision on a dispute the system sent to them (ADR-0027)."""

    decision: Literal["approve", "reject"]
    reviewer_id: str
    note: str = Field(min_length=5, max_length=500)  # why: required, for the audit trail
    decided_at: datetime
    amount: Money | None = None  # for approve: the ledger's figure


class RefundPayment(Contract):
    """The ledger's confirmation that an approved refund was paid. Copied from the ledger's
    answer; the refund exists in the ledger under `refund_id`."""

    refund_id: str
    amount: Money
    currency: Currency
    executed_at: datetime
    idempotency_key: str


# --- End-to-end result ------------------------------------------------------------------------


class TriageResult(Contract):
    """What the supervisor returns for one dispute: the outcome and how it was reached."""

    dispute_id: UUID
    transaction_id: TransactionId
    status: DisputeStatus
    verdict: DisputeVerdict | None = None  # the LLM's recommendation, if one was reached
    approval: RefundApproval | None = None  # the deterministic policy decision, for refunds
    payment: RefundPayment | None = None  # set once the ledger confirms the refund was paid
    incident: KnownIncident | None = None  # set when a confirmed incident decided it, without a model
    review: "ReviewDecision | None" = None  # set when a person decided it (ADR-0027)
    fraud: FraudAssessment | None = None
    ledger: LedgerReconciliation | None = None
    customer_message: str  # chosen by code from established facts; never model-written text
    escalation_reason: str | None = None  # for operations: why the dispute needs a person
    steps: list[str] = Field(default_factory=list)  # the path taken through the graph, in order
    trace_url: str | None = None  # Langfuse trace for this run


class DisputeView(Contract):
    """A dispute as returned by the API: the stored record, readable while it is still running."""

    dispute_id: UUID
    transaction_id: TransactionId
    status: DisputeStatus  # business status: what the customer is told
    execution_status: ExecutionStatus  # for operations; a customer-facing app would not show it
    customer_message: str
    result: TriageResult | None = None  # present once the run has finished
    created_at: datetime
    updated_at: datetime


TriageResult.model_rebuild()  # resolve the forward reference to ReviewDecision
