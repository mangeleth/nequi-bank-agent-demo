"""Known incidents in Core Systems (ADR-0022): which transactions a confirmed incident covers.
Every test runs against the in-memory ledger and, under `make test-db`, the PostgreSQL ledger."""

import os
from contextlib import asynccontextmanager

import httpx
import pytest

from services.core_systems.app import app

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
INCIDENT = "INC-20261001-01"


@pytest.fixture(params=["in_memory", pytest.param("postgres", marks=pytest.mark.skipif(
    not DATABASE_URL, reason="needs a PostgreSQL: run `make test-db`"))], autouse=True)
async def backend(request, monkeypatch):
    monkeypatch.setenv("CORE_SYSTEMS_BACKEND", request.param)
    if request.param == "postgres":
        monkeypatch.setenv("LEDGER_DATABASE_URL", DATABASE_URL)
        import psycopg

        async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
            await conn.execute("DROP TABLE IF EXISTS ledger_refunds, ledger_transactions, incidents")
    return request.param


@asynccontextmanager
async def core():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core-systems") as http:
            yield http


async def covering(http, tx, customer):
    return await http.get(f"/v1/incidents/covering/{tx}", headers={"X-Customer-Id": customer})


@pytest.mark.parametrize(("tx", "customer", "covered", "why"), [
    ("TX-20261001000009", "user-1002", True, "failed with the incident's code, in the window"),
    ("TX-20261001000010", "user-1003", True, "the same, for a customer who never disputes"),
    ("TX-20261001000011", "user-1002", False, "the same failure, but after the window"),
    ("TX-20261001000012", "user-1002", False, "to the same bank in the window, but it did not fail"),
    ("TX-20261001000001", "user-1001", False, "an ordinary failed transfer"),
])
async def test_which_transactions_the_incident_covers(tx, customer, covered, why):
    async with core() as http:
        response = await covering(http, tx, customer)
    assert (response.status_code == 200) is covered, why
    if covered:
        body = response.json()
        assert (body["incident_id"], body["failure_code"], body["recipient_bank"]) == (
            INCIDENT, "INTERBANK_TIMEOUT", "BANCO_ANDINO")
        assert body["confirmed_by"]  # a person confirmed it


async def test_another_customers_covered_transaction_looks_like_no_incident():
    async with core() as http:
        theirs = await covering(http, "TX-20261001000010", "user-1002")  # user-1003's transaction
    assert theirs.status_code == 404  # the same answer as "not covered": nothing leaks


async def test_the_batch_job_gets_exactly_the_covered_transactions():
    async with core() as http:
        response = await http.get(f"/v1/incidents/{INCIDENT}/transactions")
    assert response.status_code == 200
    assert [t["transaction_id"] for t in response.json()] == ["TX-20261001000009", "TX-20261001000010"]
    assert {t["customer_id"] for t in response.json()} == {"user-1002", "user-1003"}


@pytest.mark.parametrize(("incident", "status"), [("INC-20991231-99", 404), ("not-an-incident", 422)])
async def test_unknown_or_malformed_incident(incident, status):
    async with core() as http:
        assert (await http.get(f"/v1/incidents/{incident}/transactions")).status_code == status


async def test_transactions_now_carry_the_bank_and_the_failure_code():
    async with core() as http:
        tx = (await http.get("/v1/core-banking/transactions/TX-20261001000009",
                             headers={"X-Customer-Id": "user-1002"})).json()
    assert (tx["recipient_bank"], tx["failure_code"]) == ("BANCO_ANDINO", "INTERBANK_TIMEOUT")


async def test_a_ledger_created_before_this_step_is_upgraded_in_place(backend):
    """The cluster's ledger was created without the new columns and rows. Starting Core Systems
    must add them without touching what is there (a refund already paid stays paid)."""
    if backend != "postgres":
        pytest.skip("only a stored ledger outlives an upgrade")
    import psycopg

    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("""
            CREATE TABLE ledger_transactions (
                transaction_id text PRIMARY KEY, customer_id text NOT NULL, recipient_account text NOT NULL,
                amount numeric(15,2) NOT NULL, currency text NOT NULL, created_at timestamptz NOT NULL,
                settlement_status text NOT NULL, debited_amount numeric(15,2) NOT NULL,
                credited_amount numeric(15,2) NOT NULL)""")
        await conn.execute("""
            INSERT INTO ledger_transactions VALUES ('TX-20261001000001', 'user-1001', '****4821', 50000, 'COP',
                '2026-10-01T09:00:00Z', 'reversed', 50000, 50000)""")  # refunded before the upgrade

    async with core() as http:
        refunded = (await http.get("/v1/core-banking/transactions/TX-20261001000001",
                                   headers={"X-Customer-Id": "user-1001"})).json()
        assert refunded["settlement_status"] == "reversed"  # untouched
        assert refunded["failure_code"] == "PROCESSING_ERROR"  # backfilled
        assert (await covering(http, "TX-20261001000009", "user-1002")).status_code == 200  # new rows added


async def test_incident_lookups_are_not_mcp_tools():
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"X-Customer-Id": "user-1001"})
        async with Client(streamable_http_client("http://core-systems/mcp", http_client=http)) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
    assert tools == {"get_transaction", "get_refund_history"}
