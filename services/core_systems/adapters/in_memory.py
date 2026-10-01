"""In-memory adapters backed by synthetic fixtures. The only 'mock' part of this service."""

from datetime import timedelta
from decimal import Decimal

from services.core_systems.adapters.fixtures import AUTO_REFUNDS, NOW, RISK_SIGNALS, TRANSACTIONS
from services.core_systems.models import RefundHistory, RiskSignals, Transaction


class InMemoryLedger:
    async def get_transaction(self, customer_id: str, transaction_id: str) -> Transaction | None:
        row = TRANSACTIONS.get(transaction_id)
        if row is None or row["customer_id"] != customer_id:
            return None
        return Transaction(**row)

    async def get_refund_history(self, customer_id: str, window_days: int) -> RefundHistory:
        since = NOW - timedelta(days=window_days)
        recent = [amount for days_ago, amount in AUTO_REFUNDS.get(customer_id, [])
                  if NOW - timedelta(days=days_ago) >= since]
        return RefundHistory(
            customer_id=customer_id,
            window_days=window_days,
            auto_refund_count=len(recent),
            auto_refund_total=sum(recent, Decimal("0.00")),
        )

    async def ping(self) -> bool:
        return True


class InMemoryRisk:
    async def get_signals(self, customer_id: str, transaction_id: str) -> RiskSignals | None:
        row = TRANSACTIONS.get(transaction_id)
        if row is None or row["customer_id"] != customer_id:
            return None
        return RiskSignals(transaction_id=transaction_id, **RISK_SIGNALS[transaction_id])

    async def ping(self) -> bool:
        return True
