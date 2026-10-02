"""How the supervisor copes with sub-agents and Core Systems that misbehave: wrong status,
garbage answers, timeouts, dropped connections. Every case must become SpecialistUnavailable,
which the graph retries once and then escalates; none may crash the request.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from services.supervisor.clients import HttpSpecialists, SpecialistUnavailable
from shared.auth import CallerIdentity
from shared.schemas import DisputeRequest

DISPUTE = DisputeRequest(transaction_id="TX-20261001000001", reason="failed_transfer", claimed_amount="50000.00")
CALLER = CallerIdentity(user_id="user-1001", token_id="login-1", expires_at=datetime.now(UTC))
GOOD_FRAUD = {"transaction_id": DISPUTE.transaction_id, "risk_score": 0.08, "risk_level": "low", "rationale": "ok"}


def specialists(handler) -> HttpSpecialists:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1)
    return HttpSpecialists(http, fraud_url="http://fraud-agent", ledger_url="http://ledger-agent",
                           core_url="http://core-systems")


def timeout(request):
    raise httpx.ReadTimeout("the agent never answered")


def connection_refused(request):
    raise httpx.ConnectError("connection refused")


STUBBORN_AGENTS = {
    "HTTP 500": lambda request: httpx.Response(500, text="boom"),
    "HTTP 502 escalate": lambda request: httpx.Response(502, json={"detail": "assessment unavailable"}),
    "not JSON": lambda request: httpx.Response(200, text="<html>oops</html>"),
    "wrong shape": lambda request: httpx.Response(200, json={"hello": "world"}),
    "contradictory fields": lambda request: httpx.Response(200, json=GOOD_FRAUD | {"risk_score": 0.95}),
    "invented field": lambda request: httpx.Response(200, json=GOOD_FRAUD | {"refund_approved": True}),
    "empty body": lambda request: httpx.Response(200, content=b""),
    "timeout": timeout,
    "connection refused": connection_refused,
}


@pytest.mark.parametrize("handler", STUBBORN_AGENTS.values(), ids=STUBBORN_AGENTS.keys())
async def test_misbehaving_fraud_agent_becomes_specialist_unavailable(handler):
    with pytest.raises(SpecialistUnavailable):
        await specialists(handler).assess_fraud(DISPUTE, "token")


@pytest.mark.parametrize("handler", STUBBORN_AGENTS.values(), ids=STUBBORN_AGENTS.keys())
async def test_misbehaving_ledger_agent_becomes_specialist_unavailable(handler):
    with pytest.raises(SpecialistUnavailable):
        await specialists(handler).reconcile_ledger(DISPUTE, "token")


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(503),
    lambda request: httpx.Response(200, text="not json"),
    lambda request: httpx.Response(200, json={"auto_refund_count": "many", "auto_refund_total": "x"}),
    timeout,
])
async def test_unusable_refund_history_is_never_read_as_empty(handler):
    with pytest.raises(SpecialistUnavailable):
        await specialists(handler).refund_history(CALLER, 30)


async def test_valid_answers_are_parsed_and_identity_is_forwarded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = dict(request.headers)
        if request.url.host == "fraud-agent":
            return httpx.Response(200, json=GOOD_FRAUD)
        return httpx.Response(200, json={"auto_refund_count": 2, "auto_refund_total": "65000.00"})

    client = specialists(handler)
    traceparent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert (await client.assess_fraud(DISPUTE, "the-token", traceparent)).risk_level == "low"
    history = await client.refund_history(CALLER, 30)

    assert (history.auto_refund_count, history.auto_refund_total) == (2, Decimal("65000.00"))
    assert seen["fraud-agent"]["authorization"] == "Bearer the-token"  # the agent re-verifies it
    assert seen["fraud-agent"]["traceparent"] == traceparent  # the agent's steps join our trace
    assert seen["core-systems"]["x-customer-id"] == "user-1001"


@pytest.mark.parametrize(("status", "owns"), [(200, True), (404, False)])
async def test_ownership_check(status, owns):
    client = specialists(lambda request: httpx.Response(status, json={}))
    assert await client.owns_transaction(CALLER, DISPUTE.transaction_id) is owns


async def test_ownership_check_does_not_guess_when_core_systems_is_down():
    with pytest.raises(SpecialistUnavailable):
        await specialists(lambda request: httpx.Response(500)).owns_transaction(CALLER, DISPUTE.transaction_id)


# --- Paying through the REAL Core Systems app (ADR-0020) ----------------------------------------
# These run the supervisor's client against the actual ledger API, not a fake, because the first
# deploy showed a fake can hide a contract mismatch: the client rejected the ledger's real reply
# (it has more fields than we keep) and treated a successful payment as "no answer".


def real_core_specialists():
    from services.core_systems.app import app as core_app

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=core_app), base_url="http://core-systems")
    return core_app, HttpSpecialists(http, fraud_url="http://fraud-agent", ledger_url="http://ledger-agent",
                                     core_url="http://core-systems")


async def test_pay_refund_reads_the_real_ledgers_confirmation(monkeypatch):
    monkeypatch.setenv("CORE_SYSTEMS_BACKEND", "in_memory")
    core_app, client = real_core_specialists()
    key = "dispute:5f0e7c1a-9b2d-4e6f-8a1b-3c5d7e9f0a1b"
    async with core_app.router.lifespan_context(core_app):
        first = await client.pay_refund("user-1001", DISPUTE.transaction_id, Decimal("50000.00"), key)
        replay = await client.pay_refund("user-1001", DISPUTE.transaction_id, Decimal("50000.00"), key)

    assert (first.amount, first.currency, first.idempotency_key) == (Decimal("50000.00"), "COP", key)
    assert replay == first  # the same refund: paid once


async def test_pay_refund_turns_the_real_ledgers_refusal_into_refund_refused(monkeypatch):
    from services.supervisor.clients import RefundRefused

    monkeypatch.setenv("CORE_SYSTEMS_BACKEND", "in_memory")
    core_app, client = real_core_specialists()
    async with core_app.router.lifespan_context(core_app):
        with pytest.raises(RefundRefused) as refused:
            await client.pay_refund("user-1001", DISPUTE.transaction_id, Decimal("49000.00"),
                                    "dispute:5f0e7c1a-9b2d-4e6f-8a1b-3c5d7e9f0a1b")
    assert refused.value.code == "amount_mismatch"


@pytest.mark.parametrize("reply", [
    {"transaction_id": "TX-20261001000008"},  # someone else's refund
    {"amount": "5.00"},
    {"idempotency_key": "dispute:another"},
])
async def test_a_confirmation_for_a_different_refund_is_not_trusted(reply):
    confirmation = {"refund_id": "RF-1", "transaction_id": DISPUTE.transaction_id, "customer_id": "user-1001",
                    "amount": "50000.00", "currency": "COP", "executed_at": "2026-10-02T14:00:00Z",
                    "idempotency_key": "dispute:mine"} | reply
    client = specialists(lambda request: httpx.Response(201, json=confirmation))
    with pytest.raises(SpecialistUnavailable):
        await client.pay_refund("user-1001", DISPUTE.transaction_id, Decimal("50000.00"), "dispute:mine")
