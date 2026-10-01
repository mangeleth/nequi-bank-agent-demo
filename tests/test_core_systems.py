import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

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


# --- MCP front door (/mcp) --------------------------------------------------------------------


@asynccontextmanager
async def mcp_client(customer_id: str | None, host: str = "core-systems"):
    """An MCP client talking to the Core Systems app in-process, as the given customer."""
    headers = {"X-Customer-Id": customer_id} if customer_id else {}
    # Start the app and connect inside one `async with`, so startup and shutdown share a task.
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)
        async with Client(streamable_http_client(f"http://{host}/mcp", http_client=http)) as client:
            yield client


async def call_json(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def test_mcp_tools_have_no_identity_parameter():
    async with mcp_client("user-1001") as client:
        tools = {tool.name: tool.input_schema["properties"] for tool in (await client.list_tools()).tools}
    assert set(tools) == {"get_transaction", "get_refund_history"}
    assert set(tools["get_transaction"]) == {"transaction_id"}
    assert set(tools["get_refund_history"]) == {"window_days"}


async def test_mcp_owner_reads_transaction_without_customer_id_in_the_result():
    async with mcp_client("user-1001") as client:
        body = await call_json(client, "get_transaction", {"transaction_id": "TX-20261001000001"})
    assert body["settlement_status"] == "failed" and body["debited_amount"] == "50000.00"
    assert "customer_id" not in body


async def test_mcp_other_customers_transaction_is_not_found():
    async with mcp_client("user-1002") as client:
        body = await call_json(client, "get_transaction", {"transaction_id": "TX-20261001000001"})
    assert body == {"error": "No such transaction for this customer."}


async def test_mcp_refund_history_is_scoped_to_the_caller():
    async with mcp_client("user-1003") as client:
        body = await call_json(client, "get_refund_history", {"window_days": 30})
    assert body == {"window_days": 30, "auto_refund_count": 3, "auto_refund_total": "65000.00"}


@pytest.mark.parametrize("customer_id", [None, "admin"])
async def test_mcp_call_without_a_valid_customer_is_an_error(customer_id):
    async with mcp_client(customer_id) as client:
        result = await client.call_tool("get_transaction", {"transaction_id": "TX-20261001000001"})
    assert result.is_error


async def test_mcp_rejects_malformed_transaction_id():
    async with mcp_client("user-1001") as client:
        result = await client.call_tool("get_transaction", {"transaction_id": "TX-1/../../readyz"})
    assert result.is_error


async def test_mcp_rejects_unexpected_host_header():
    with pytest.raises(Exception):  # noqa: B017 - DNS rebinding protection refuses the connection
        async with mcp_client("user-1001", host="evil.example") as client:
            await client.list_tools()
