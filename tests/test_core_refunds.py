"""Refund execution in Core Systems: the first write to the ledger, and the only endpoint that
moves money. The rules under test are the ledger's own, whatever the caller decided upstream."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from services.core_systems.app import app

FAILED_TX = "TX-20261001000001"  # user-1001: 50.000 debited, 0 credited, failed
SETTLED_TX = "TX-20261001000003"  # user-1001
OTHERS_TX = "TX-20261001000004"  # user-1002
PENDING_TX = "TX-20261001000005"  # user-1002
REVERSED_TX = "TX-20261001000006"  # user-1003: already refunded
KEY = "dispute:5f0e7c1a9b2d4e6f8a1b3c5d7e9f0a1b"
URL = "/v1/core-banking/refunds"


@asynccontextmanager
async def core():
    """A fresh Core Systems (its own copy of the ledger), driven by an async HTTP client."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core-systems") as http:
            yield http


async def refund(http, tx=FAILED_TX, amount="50000.00", key=KEY, customer="user-1001"):
    return await http.post(URL, json={"transaction_id": tx, "amount": amount},
                           headers={"X-Customer-Id": customer, "Idempotency-Key": key})


async def history(http, customer="user-1001") -> tuple[int, str]:
    body = (await http.get("/v1/core-banking/refund-history", headers={"X-Customer-Id": customer})).json()
    return body["auto_refund_count"], body["auto_refund_total"]


async def transaction(http, tx=FAILED_TX, customer="user-1001") -> dict:
    return (await http.get(f"/v1/core-banking/transactions/{tx}", headers={"X-Customer-Id": customer})).json()


# --- Paying once ---------------------------------------------------------------------------------


async def test_refund_moves_the_money_and_records_it():
    async with core() as http:
        response = await refund(http)
        body = response.json()

        assert response.status_code == 201 and "idempotent-replay" not in response.headers
        assert (body["transaction_id"], body["amount"], body["currency"]) == (FAILED_TX, "50000.00", "COP")
        assert body["refund_id"].startswith("RF-") and body["idempotency_key"] == KEY
        assert (await transaction(http))["settlement_status"] == "reversed"
        assert await history(http) == (1, "50000.00")


async def test_the_same_key_again_returns_the_same_refund_and_pays_nothing():
    async with core() as http:
        first = await refund(http)
        again = await refund(http)

        assert (first.status_code, again.status_code) == (201, 200)
        assert again.headers["idempotent-replay"] == "true"
        assert again.json() == first.json()  # the same refund_id
        assert await history(http) == (1, "50000.00")  # paid once


async def test_ten_simultaneous_requests_with_one_key_pay_once():
    async with core() as http:
        responses = await asyncio.gather(*(refund(http) for _ in range(10)))

        assert sorted(r.status_code for r in responses) == [200] * 9 + [201]
        assert len({r.json()["refund_id"] for r in responses}) == 1
        assert await history(http) == (1, "50000.00")


async def test_a_different_key_cannot_refund_the_same_transaction_again():
    # The key protects against retries. The ledger's own state protects against everything else.
    async with core() as http:
        assert (await refund(http, key=KEY)).status_code == 201
        second = await refund(http, key="dispute:another-key-0123456789abcdef")

        assert second.status_code == 422 and second.json()["detail"]["code"] == "already_refunded"
        assert await history(http) == (1, "50000.00")


# --- The key cannot be reused for something else -----------------------------------------------------


@pytest.mark.parametrize("changed", [{"amount": "49999.00"}, {"tx": "TX-20261001000008"}])
async def test_the_same_key_for_a_different_refund_is_a_conflict(changed):
    async with core() as http:
        await refund(http)
        conflict = await refund(http, **changed)
        assert conflict.status_code == 409
        assert await history(http) == (1, "50000.00")


# --- The ledger's own rules -----------------------------------------------------------------------------


@pytest.mark.parametrize(("tx", "amount", "customer", "status", "code"), [
    (FAILED_TX, "5000000.00", "user-1001", 422, "amount_mismatch"),  # more than the ledger shows owed
    (FAILED_TX, "1.00", "user-1001", 422, "amount_mismatch"),
    (SETTLED_TX, "80000.00", "user-1001", 422, "not_refundable"),
    (PENDING_TX, "20000.00", "user-1002", 422, "not_refundable"),
    (REVERSED_TX, "40000.00", "user-1003", 422, "already_refunded"),
    (OTHERS_TX, "30000.00", "user-1001", 404, "transaction_not_found"),  # someone else's transaction
    ("TX-99999999999999", "1.00", "user-1001", 404, "transaction_not_found"),
])
async def test_the_ledger_refuses_what_it_does_not_owe(tx, amount, customer, status, code):
    async with core() as http:
        before = await history(http, customer)
        response = await refund(http, tx=tx, amount=amount, customer=customer)

        assert response.status_code == status and response.json()["detail"]["code"] == code
        assert await history(http, customer) == before  # nothing moved


async def test_a_refused_refund_does_not_use_up_the_key():
    async with core() as http:
        assert (await refund(http, amount="1.00")).status_code == 422
        assert (await refund(http, amount="50000.00")).status_code == 201  # the correct request still works


@pytest.mark.parametrize("headers", [
    {"X-Customer-Id": "user-1001"},  # no key: a retry could pay twice, so it is not accepted at all
    {"X-Customer-Id": "user-1001", "Idempotency-Key": "short"},
    {"Idempotency-Key": KEY},  # no customer
])
async def test_refund_requires_a_key_and_a_customer(headers):
    async with core() as http:
        response = await http.post(URL, json={"transaction_id": FAILED_TX, "amount": "50000.00"}, headers=headers)
        assert response.status_code == 422
        assert await history(http) == (0, "0.00")


@pytest.mark.parametrize("amount", [50000.0, "50000.001", "-50000.00", "0"])
async def test_refund_amount_must_be_exact_money(amount):
    async with core() as http:
        response = await http.post(URL, json={"transaction_id": FAILED_TX, "amount": amount},
                                   headers={"X-Customer-Id": "user-1001", "Idempotency-Key": KEY})
        assert response.status_code == 422


# --- No model can reach it ----------------------------------------------------------------------------


async def test_refund_is_not_offered_as_an_mcp_tool():
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"X-Customer-Id": "user-1001"})
        async with Client(streamable_http_client("http://core-systems/mcp", http_client=http)) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
    assert tools == {"get_transaction", "get_refund_history"}  # read-only: nothing that moves money


async def test_each_core_systems_start_has_its_own_ledger():
    # Documents the limit of the in-memory adapter: state is per process (see ADR-0019).
    async with core() as http:
        await refund(http)
        assert await history(http) == (1, "50000.00")
    async with core() as http:
        assert await history(http) == (0, "0.00")
