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
| TX-20261001000008  | user-1001 | 50.000 debited, never credited         | auto-approved refund       |

Known incident INC-20261001-01 (ADR-0022): transfers to Banco Andino timed out 09:00-09:40.

| TX-20261001000009  | user-1002 | 35.000 to Banco Andino, 09:10, timed out | covered: decided without a model |
| TX-20261001000010  | user-1003 | 60.000 to Banco Andino, 09:25, timed out | covered, never disputed: the batch refunds it |
| TX-20261001000011  | user-1002 | 20.000 to Banco Andino, 10:30, timed out | NOT covered (after the window) |
| TX-20261001000012  | user-1002 | 15.000 to Banco Andino, 09:20, settled   | NOT covered (it did not fail)  |
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shared.schemas import SettlementStatus

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _tx(tx_id, customer, recipient, amount, status, credited, hours_ago, *, bank="NEQUI", failure_code=None):
    if failure_code is None and status in (SettlementStatus.FAILED, SettlementStatus.REVERSED):
        failure_code = "PROCESSING_ERROR"
    return {
        "transaction_id": tx_id,
        "customer_id": customer,
        "recipient_account": recipient,
        "recipient_bank": bank,
        "amount": Decimal(amount),
        "currency": "COP",
        "created_at": NOW - timedelta(hours=hours_ago),
        "settlement_status": status,
        "debited_amount": Decimal(amount),
        "credited_amount": Decimal(credited),
        "failure_code": failure_code,
    }


ANDINO = {"bank": "BANCO_ANDINO", "failure_code": "INTERBANK_TIMEOUT"}


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
        _tx("TX-20261001000008", "user-1001", "****4821", "50000.00", SettlementStatus.FAILED, "0.00", 4),
        # Known incident: NOW is 12:00, so 2h50m ago is 09:10.
        _tx("TX-20261001000009", "user-1002", "****6610", "35000.00", SettlementStatus.FAILED, "0.00", 2 + 50 / 60, **ANDINO),
        _tx("TX-20261001000010", "user-1003", "****6610", "60000.00", SettlementStatus.FAILED, "0.00", 2 + 35 / 60, **ANDINO),
        _tx("TX-20261001000011", "user-1002", "****6611", "20000.00", SettlementStatus.FAILED, "0.00", 1.5, **ANDINO),
        _tx("TX-20261001000012", "user-1002", "****6612", "15000.00", SettlementStatus.SETTLED, "15000.00", 2 + 40 / 60,
            bank="BANCO_ANDINO"),
    ]
}

# Incidents confirmed by operations (ADR-0022). Confirmed once, for every affected transaction.
INCIDENTS = {
    "INC-20261001-01": {
        "incident_id": "INC-20261001-01",
        "title": "Interbank transfers to Banco Andino timed out",
        "failure_code": "INTERBANK_TIMEOUT",
        "recipient_bank": "BANCO_ANDINO",
        "window_start": NOW - timedelta(hours=3),  # 09:00
        "window_end": NOW - timedelta(hours=2, minutes=20),  # 09:40, exclusive
        "confirmed_by": "operations-lead (demo)",
        "confirmed_at": NOW - timedelta(hours=1, minutes=55),  # 10:05
    },
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
    "TX-20261001000008": dict(engine_score=0.07, recipient_known=True, recipient_account_age_days=900,
                              amount_vs_customer_avg=0.9, new_device_last_24h=False, transfers_last_24h=2),
    "TX-20261001000009": dict(engine_score=0.09, recipient_known=True, recipient_account_age_days=600,
                              amount_vs_customer_avg=1.0, new_device_last_24h=False, transfers_last_24h=2),
    "TX-20261001000010": dict(engine_score=0.06, recipient_known=True, recipient_account_age_days=800,
                              amount_vs_customer_avg=1.2, new_device_last_24h=False, transfers_last_24h=1),
    "TX-20261001000011": dict(engine_score=0.10, recipient_known=True, recipient_account_age_days=600,
                              amount_vs_customer_avg=0.6, new_device_last_24h=False, transfers_last_24h=3),
    "TX-20261001000012": dict(engine_score=0.05, recipient_known=True, recipient_account_age_days=500,
                              amount_vs_customer_avg=0.5, new_device_last_24h=False, transfers_last_24h=3),
}

# Past automatic refunds per customer: (days_ago, amount).
AUTO_REFUNDS = {
    "user-1001": [],
    "user-1002": [],
    "user-1003": [(2, Decimal("40000.00")), (9, Decimal("15000.00")), (20, Decimal("10000.00")),
                  (45, Decimal("30000.00"))],  # the 45-day-old one is outside a 30-day window
}
