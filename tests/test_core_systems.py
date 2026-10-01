import pytest
from fastapi.testclient import TestClient

from services.core_systems.app import _build_adapters, app

OWNER = {"X-Customer-Id": "user-1001"}
OTHER = {"X-Customer-Id": "user-1002"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # `with` runs the lifespan, which wires the adapters
        yield c


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_owner_reads_failed_transfer(client):
    body = client.get("/v1/core-banking/transactions/TX-20261001000001", headers=OWNER).json()
    assert body["settlement_status"] == "failed"
    assert body["debited_amount"] == "50000.00"  # money stays a string in JSON
    assert body["credited_amount"] == "0.00"


def test_other_customers_transaction_is_404_not_403(client):
    # IDOR: user-1002 must not see user-1001's transaction, nor learn that it exists.
    owned_by_other = client.get("/v1/core-banking/transactions/TX-20261001000001", headers=OTHER)
    missing = client.get("/v1/core-banking/transactions/TX-99999999999999", headers=OTHER)
    assert owned_by_other.status_code == missing.status_code == 404
    assert owned_by_other.json() == missing.json()


def test_risk_signals_are_owner_scoped_too(client):
    assert client.get("/v1/risk/transactions/TX-20261001000001/signals", headers=OTHER).status_code == 404
    body = client.get("/v1/risk/transactions/TX-20261001000004/signals", headers=OTHER).json()
    assert body["engine_score"] == 0.86 and body["new_device_last_24h"] is True


@pytest.mark.parametrize("headers", [{}, {"X-Customer-Id": "admin"}, {"X-Customer-Id": "user-1; DROP TABLE"}])
def test_missing_or_malformed_customer_is_rejected(client, headers):
    assert client.get("/v1/core-banking/transactions/TX-20261001000001", headers=headers).status_code == 422


def test_malformed_transaction_id_is_rejected(client):
    assert client.get("/v1/core-banking/transactions/TX-1;ignore", headers=OWNER).status_code == 422


@pytest.mark.parametrize(
    ("customer", "window", "count", "total"),
    [
        ("user-1001", 30, 0, "0.00"),
        ("user-1003", 30, 3, "65000.00"),  # 45-day-old refund excluded
        ("user-1003", 60, 4, "95000.00"),
    ],
)
def test_refund_history_window(client, customer, window, count, total):
    body = client.get(f"/v1/core-banking/refund-history?window_days={window}",
                      headers={"X-Customer-Id": customer}).json()
    assert (body["auto_refund_count"], body["auto_refund_total"]) == (count, total)


def test_unknown_backend_fails_fast():
    with pytest.raises(ValueError, match="unknown CORE_SYSTEMS_BACKEND"):
        _build_adapters("postgres")


def test_adapter_can_be_swapped_without_touching_the_api(client):
    class DownLedger:
        async def ping(self):
            return False

    original = app.state.ledger
    app.state.ledger = DownLedger()  # any object satisfying the port works
    try:
        assert client.get("/readyz").status_code == 503
    finally:
        app.state.ledger = original
