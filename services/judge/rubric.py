"""What the judge grades, and what it returns (ADR-0026).

The deterministic checks prove the DECISION (status, amount, policy route). They cannot tell
whether the EXPLANATION is true: "the transfer failed for insufficient funds" passes every status
check when the record says "processing error". The judge grades the words the models wrote,
against the records, with three criteria:

    groundedness   every factual claim is supported by the evidence; nothing is invented
    completeness   it answers the customer's question, or says what is not known
    clarity        a customer could understand it; extra length earns no credit

Each criterion is pass or fail with a short reason. The judge measures quality and raises alerts.
It never blocks or changes a decision.
"""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Criterion(StrEnum):
    GROUNDEDNESS = "groundedness"
    COMPLETENESS = "completeness"
    CLARITY = "clarity"


# Changes to the rubric or the prompt are versioned, and calibration results record the version.
# Changes come only from TUNING cases. A held-out case whose failure is used to change the prompt is
# moved to tuning, and new held-out cases are written (see docs/adr/0026).
PROMPT_VERSION = "v3"

RUBRIC = {
    Criterion.GROUNDEDNESS: (
        "Every factual claim about the records (amounts, statuses, causes, dates, the recipient, risk "
        "signals) agrees with the EVIDENCE. FAIL if a claim contradicts the evidence, or states a fact the "
        "evidence does not contain, such as a different cause, a prediction, or a promise about the future. "
        "Only facts count here: the decision itself (refund recommended, no refund, sent to a person) is "
        "the system's conclusion, not a fact, so judge only whether the reasons given for it are true. "
        "Wording, jargon, and length are never a groundedness problem. Saying something is unknown is "
        "never a reason to fail."),
    Criterion.COMPLETENESS: (
        "It states the outcome for the customer (refund recommended, no refund due, not decided yet, or "
        "sent to a person) AND the main reason for it, or says plainly that the evidence does not settle "
        "it. That is all that is required: timelines, payment steps, and how the process continues are "
        "NOT required. FAIL only if the outcome or its reason is missing, or the question is avoided."),
    Criterion.CLARITY: (
        "A customer without banking knowledge could understand it. FAIL for field names, code-like text, "
        "or internal jargon the customer would not understand, or for padding and repetition that hide "
        "the answer. Short answers are fine; number formats such as 50.000 COP are fine. Facts and "
        "completeness are never a clarity problem."),
}

# The bank's own definitions: general knowledge every reviewer has, given to the judge so it does
# not have to guess what the records mean (added in v2 after tuning cases failed on "low risk").
DEFINITIONS = (
    "- Risk: engine_score below 0.4 is low risk, 0.4 to 0.7 is medium, 0.7 or above is high.\n"
    "- debited_amount left the customer's account. On a settled or failed transfer, credited_amount "
    "reached the recipient. On a reversed transfer, credited_amount is the amount RETURNED to the "
    "customer: the money is back with them.\n"
    "- settlement_status: settled = completed; pending = still in progress; failed = did not complete; "
    "reversed = failed and the amount was already returned to the customer.\n"
    "- failure_code is the only known cause. Any other cause (a bank rejection, insufficient funds, "
    "maintenance, a network problem...) is invented, however plausible, unless the evidence states it."
)

Reason = Annotated[str, Field(min_length=1, max_length=300)]


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    reason: Reason


class JudgeOutput(BaseModel):
    """What the judge model returns: one result per criterion. Field order matters: the model
    writes the reason before it can be tempted to rationalise a verdict."""

    model_config = ConfigDict(extra="forbid")

    groundedness: CriterionResult
    completeness: CriterionResult
    clarity: CriterionResult

    @property
    def passed(self) -> bool:
        return self.groundedness.passed and self.completeness.passed and self.clarity.passed

    def result(self, criterion: Criterion) -> CriterionResult:
        return getattr(self, criterion.value)


class JudgeCase(BaseModel):
    """One thing to judge: the customer's question, what the models wrote, and the evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: dict  # the dispute: reason, claimed amount, description
    answer: dict[str, str]  # what each model wrote, e.g. {"explanation": ..., "ledger_summary": ...}
    evidence: dict  # the records, read by code from Core Systems: transaction, risk signals
