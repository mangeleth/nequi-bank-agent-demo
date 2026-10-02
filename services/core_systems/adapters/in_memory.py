"""In-memory adapters backed by synthetic fixtures. The only 'mock' part of this service.

Each `InMemoryLedger` holds its own copy of the data, so a refund executed in one instance is
not seen by another. That is fine for tests and one process, and wrong for several replicas:
a ledger that is written to needs one shared store (see ADR-0019).
"""

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from services.core_systems.adapters.fixtures import AUTO_REFUNDS, INCIDENTS, RISK_SIGNALS, TRANSACTIONS
from services.core_systems.models import Incident, Refund, RefundHistory, RiskSignals, Transaction
from services.core_systems.ports import IdempotencyConflict, RefundRejected
from shared.schemas import SettlementStatus


def covers(incident: dict, tx: dict) -> bool:
    """The incident rule (ADR-0022): same failure code, same recipient bank, inside the window.
    The PostgreSQL adapter applies the same rule in SQL; tests run both."""
    return (tx["failure_code"] == incident["failure_code"] and tx["recipient_bank"] == incident["recipient_bank"]
            and incident["window_start"] <= tx["created_at"] < incident["window_end"])


class InMemoryLedger:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self._transactions = copy.deepcopy(TRANSACTIONS)
        self._incidents = copy.deepcopy(INCIDENTS)
        # Past automatic refunds per customer, as (when, amount)
        self._refund_history = {customer: [(now - timedelta(days=days_ago), amount) for days_ago, amount in past]
                                for customer, past in AUTO_REFUNDS.items()}
        self._refunds_by_key: dict[str, Refund] = {}
        self._lock = asyncio.Lock()  # one refund at a time, as a database transaction would ensure

    async def get_transaction(self, customer_id: str, transaction_id: str) -> Transaction | None:
        row = self._transactions.get(transaction_id)
        if row is None or row["customer_id"] != customer_id:
            return None
        return Transaction(**row)

    async def get_refund_history(self, customer_id: str, window_days: int) -> RefundHistory:
        since = datetime.now(UTC) - timedelta(days=window_days)
        recent = [amount for when, amount in self._refund_history.get(customer_id, []) if when >= since]
        return RefundHistory(
            customer_id=customer_id,
            window_days=window_days,
            auto_refund_count=len(recent),
            auto_refund_total=sum(recent, Decimal("0.00")),
        )

    async def incident_for(self, customer_id: str, transaction_id: str) -> Incident | None:
        tx = self._transactions.get(transaction_id)
        if tx is None or tx["customer_id"] != customer_id:
            return None
        for incident in self._incidents.values():
            if covers(incident, tx):
                return Incident(**incident)
        return None

    async def affected_transactions(self, incident_id: str) -> list[Transaction] | None:
        incident = self._incidents.get(incident_id)
        if incident is None:
            return None
        return [Transaction(**tx) for tx in sorted(self._transactions.values(), key=lambda t: t["transaction_id"])
                if covers(incident, tx)]

    async def execute_refund(
        self, customer_id: str, transaction_id: str, amount: Decimal, idempotency_key: str
    ) -> tuple[Refund, bool]:
        async with self._lock:
            # 1. Seen this key before? Then nothing moves: return what was done the first time.
            previous = self._refunds_by_key.get(idempotency_key)
            if previous is not None:
                same_request = (previous.customer_id, previous.transaction_id, previous.amount) == (
                    customer_id, transaction_id, amount)
                if not same_request:
                    raise IdempotencyConflict("this idempotency key was used for a different refund")
                return previous, True

            # 2. The ledger's own rules. They hold even if the caller's checks were wrong.
            row = self._transactions.get(transaction_id)
            if row is None or row["customer_id"] != customer_id:
                raise RefundRejected("transaction_not_found", "transaction not found")
            if row["settlement_status"] == SettlementStatus.REVERSED:
                raise RefundRejected("already_refunded", "this transaction was already refunded")
            if row["settlement_status"] != SettlementStatus.FAILED:
                raise RefundRejected("not_refundable", f"a {row['settlement_status'].value} transaction cannot be refunded")
            owed = row["debited_amount"] - row["credited_amount"]
            if amount != owed:
                raise RefundRejected("amount_mismatch", f"the ledger shows {owed} owed, not {amount}")

            # 3. Move the money and record it, together.
            refund = Refund(
                refund_id=f"RF-{uuid.uuid4().hex[:16]}", transaction_id=transaction_id, customer_id=customer_id,
                amount=amount, currency=row["currency"], executed_at=datetime.now(UTC),
                idempotency_key=idempotency_key,
            )
            row["settlement_status"] = SettlementStatus.REVERSED
            row["credited_amount"] = row["debited_amount"]  # the amount is back with the customer
            self._refund_history.setdefault(customer_id, []).append((refund.executed_at, amount))
            self._refunds_by_key[idempotency_key] = refund
            return refund, False

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
