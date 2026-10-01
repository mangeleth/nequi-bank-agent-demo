"""Synthetic data for the in-memory adapter (ADR-0003: no real customer data). One scenario per transaction.

| Transaction        | Customer  | Story                                  | Expected outcome           |
|--------------------|-----------|----------------------------------------|----------------------------|
| TX-20261001000001  | user-1001 | 50.000 debited, never credited         | auto-approved refund       |
| TX-20261001000002  | user-1001 | 450.000 debited, never credited        | human (over amount limit)  |
| TX-20261001000003  | user-1001 | 80.000 settled normally                | no action                  |
| TX-20261001000004  | user-1002 | 30.000 failed, new recipient + device  | human / escalate fraud     |
| TX-20261001000005  | user-1002 | 20.000 still in flight                 | no action (wait)           |
| TX-20261001000006  | user-1003 | 40.000 failed, already reversed        | no action (already refunded) |
| TX-20261001000007  | user-1003 | 25.000 failed, 3 auto-refunds already  | human (count limit)        |
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shared.schemas import SettlementStatus

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _tx(tx_id, customer, recipient, amount, status, credited, hours_ago):
    return {
        "transaction_id": tx_id,
        "customer_id": customer,
        "recipient_account": recipient,
        "amount": Decimal(amount),
        "currency": "COP",
        "created_at": NOW - timedelta(hours=hours_ago),
        "settlement_status": status,
        "debited_amount": Decimal(amount),
        "credited_amount": Decimal(credited),
    }


TRANSACTIONS = {
    tx["transaction_id"]: tx
    for tx in [
        _tx("TX-20261001000001", "user-1001", "****4821", "50000.00", SettlementStatus.FAILED, "0.00", 3),
        _tx("TX-20261001000002", "user-1001", "****7710", "450000.00", SettlementStatus.FAILED, "0.00", 5),
        _tx("TX-20261001000003", "user-1001", "****4821", "80000.00", SettlementStatus.SETTLED, "80000.00", 30),
        _tx("TX-20261001000004", "user-1002", "****0093", "30000.00", SettlementStatus.FAILED, "0.00", 1),
        _tx("TX-20261001000005", "user-1002", "****5512", "20000.00", SettlementStatus.PENDING, "0.00", 0),
        _tx("TX-20261001000006", "user-1003", "****3307", "40000.00", SettlementStatus.REVERSED, "40000.00", 48),
        _tx("TX-20261001000007", "user-1003", "****3307", "25000.00", SettlementStatus.FAILED, "0.00", 2),
    ]
}

# What the risk engine knows about each transaction (raw signals; the Fraud Agent interprets them).
RISK_SIGNALS = {
    "TX-20261001000001": dict(engine_score=0.08, recipient_known=True, recipient_account_age_days=900,
                              amount_vs_customer_avg=0.9, new_device_last_24h=False, transfers_last_24h=2),
    "TX-20261001000002": dict(engine_score=0.15, recipient_known=True, recipient_account_age_days=1200,
                              amount_vs_customer_avg=2.1, new_device_last_24h=False, transfers_last_24h=1),
    "TX-20261001000003": dict(engine_score=0.05, recipient_known=True, recipient_account_age_days=900,
                              amount_vs_customer_avg=1.0, new_device_last_24h=False, transfers_last_24h=3),
    "TX-20261001000004": dict(engine_score=0.86, recipient_known=False, recipient_account_age_days=2,
                              amount_vs_customer_avg=4.5, new_device_last_24h=True, transfers_last_24h=9),
    "TX-20261001000005": dict(engine_score=0.10, recipient_known=True, recipient_account_age_days=400,
                              amount_vs_customer_avg=0.8, new_device_last_24h=False, transfers_last_24h=1),
    "TX-20261001000006": dict(engine_score=0.12, recipient_known=True, recipient_account_age_days=700,
                              amount_vs_customer_avg=1.1, new_device_last_24h=False, transfers_last_24h=1),
    "TX-20261001000007": dict(engine_score=0.11, recipient_known=True, recipient_account_age_days=700,
                              amount_vs_customer_avg=0.7, new_device_last_24h=False, transfers_last_24h=1),
}

# Past automatic refunds per customer: (days_ago, amount).
AUTO_REFUNDS = {
    "user-1001": [],
    "user-1002": [],
    "user-1003": [(2, Decimal("40000.00")), (9, Decimal("15000.00")), (20, Decimal("10000.00")),
                  (45, Decimal("30000.00"))],  # the 45-day-old one is outside a 30-day window
}
