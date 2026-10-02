"""Resource representations returned by the Core Banking and Risk Engine APIs (v1)."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas import Amount, Currency, Money, Score, SettlementStatus, TransactionId

CustomerId = Annotated[str, Field(pattern=r"^user-[0-9]{4,12}$")]


class Resource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Transaction(Resource):
    transaction_id: TransactionId
    customer_id: CustomerId
    recipient_account: str = Field(pattern=r"^\*{4}[0-9]{4}$")  # masked, e.g. ****4821
    amount: Amount
    currency: Currency
    created_at: datetime
    settlement_status: SettlementStatus
    debited_amount: Amount
    credited_amount: Amount


class RiskSignals(Resource):
    transaction_id: TransactionId
    engine_score: Score
    recipient_known: bool
    recipient_account_age_days: int = Field(ge=0)
    amount_vs_customer_avg: float = Field(ge=0)  # 1.0 = this customer's usual amount
    new_device_last_24h: bool
    transfers_last_24h: int = Field(ge=0)


class RefundHistory(Resource):
    customer_id: CustomerId
    window_days: int = Field(ge=1, le=365)
    auto_refund_count: int = Field(ge=0)
    auto_refund_total: Amount


class RefundRequest(Resource):
    """What a caller asks the ledger to refund. The customer comes from the caller's identity."""

    transaction_id: TransactionId
    amount: Money


class Refund(Resource):
    """A refund the ledger has executed. Money moved exactly once per refund."""

    refund_id: str
    transaction_id: TransactionId
    customer_id: CustomerId
    amount: Money
    currency: Currency
    executed_at: datetime
    idempotency_key: str
