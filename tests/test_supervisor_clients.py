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
    assert (await client.assess_fraud(DISPUTE, "the-token")).risk_level == "low"
    history = await client.refund_history(CALLER, 30)

    assert (history.auto_refund_count, history.auto_refund_total) == (2, Decimal("65000.00"))
    assert seen["fraud-agent"]["authorization"] == "Bearer the-token"  # the agent re-verifies it
    assert seen["core-systems"]["x-customer-id"] == "user-1001"


@pytest.mark.parametrize(("status", "owns"), [(200, True), (404, False)])
async def test_ownership_check(status, owns):
    client = specialists(lambda request: httpx.Response(status, json={}))
    assert await client.owns_transaction(CALLER, DISPUTE.transaction_id) is owns


async def test_ownership_check_does_not_guess_when_core_systems_is_down():
    with pytest.raises(SpecialistUnavailable):
        await specialists(lambda request: httpx.Response(500)).owns_transaction(CALLER, DISPUTE.transaction_id)
