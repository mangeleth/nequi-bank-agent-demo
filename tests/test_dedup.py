"""Deduplication at the gate: one dispute per customer and transaction, however many times the
customer taps. The same behaviour is required of the in-memory store and the Redis store."""

import asyncio
import json

import fakeredis
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from services.supervisor.dedup import GateSettings, GateUnavailable, InMemoryGate, RedisGate, dispute_key
from services.supervisor.main import create_app
from shared.refund_policy import RefundPolicyConfig
from shared.tracing import Tracing
from tests.fakes import ScriptedChatModel
from tests.jwt_helpers import SETTINGS, bearer
from tests.test_supervisor import DISPUTE, HAPPY, URL, FakeSpecialists

KEY = dispute_key("user-1001", "TX-20261001000001")


def gates(settings: GateSettings | None = None):
    return {"memory": InMemoryGate(settings), "redis": RedisGate(fakeredis.FakeAsyncRedis(decode_responses=True), settings)}


@pytest.fixture(params=["memory", "redis"])
def gate(request):
    return gates()[request.param]


# --- The key ----------------------------------------------------------------------------------


def test_key_is_deterministic_and_reveals_nothing():
    assert dispute_key("user-1001", "TX-20261001000001") == KEY
    assert KEY.startswith("dispute:") and len(KEY) == len("dispute:") + 64
    assert "user-1001" not in KEY and "TX-" not in KEY


def test_key_differs_by_customer_and_by_transaction():
    assert dispute_key("user-1002", "TX-20261001000001") != KEY
    assert dispute_key("user-1001", "TX-20261001000002") != KEY
    assert dispute_key("user-10", "01TX-1") != dispute_key("user-1001", "TX-1")  # no boundary tricks


# --- The store (same rules for both implementations) -------------------------------------------


async def test_first_claim_wins_and_a_duplicate_sees_it_in_progress(gate):
    assert (await gate.claim(KEY)).claimed
    duplicate = await gate.claim(KEY)
    assert not duplicate.claimed and duplicate.in_progress


async def test_ten_simultaneous_claims_have_exactly_one_winner(gate):
    claims = await asyncio.gather(*(gate.claim(KEY) for _ in range(10)))
    assert sum(claim.claimed for claim in claims) == 1


async def test_after_completion_duplicates_get_the_stored_result(gate):
    await gate.claim(KEY)
    await gate.complete(KEY, '{"status": "resolved"}')
    duplicate = await gate.claim(KEY)
    assert (duplicate.claimed, duplicate.in_progress, duplicate.result) == (False, False, '{"status": "resolved"}')


async def test_release_lets_the_customer_try_again(gate):
    await gate.claim(KEY)
    await gate.release(KEY)
    assert (await gate.claim(KEY)).claimed


async def test_release_never_deletes_a_finished_result(gate):
    await gate.claim(KEY)
    await gate.complete(KEY, '{"status": "resolved"}')
    await gate.release(KEY)
    assert (await gate.claim(KEY)).result == '{"status": "resolved"}'


@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_a_crashed_run_frees_the_key_when_the_lock_expires(backend):
    gate = gates(GateSettings(lock_seconds=1))[backend]
    assert (await gate.claim(KEY)).claimed  # ...and the process dies before completing
    assert (await gate.claim(KEY)).in_progress
    await asyncio.sleep(1.2)
    assert (await gate.claim(KEY)).claimed


async def test_unreachable_redis_is_reported_not_hidden():
    class DownRedis:
        async def set(self, *args, **kwargs):
            raise RedisConnectionError("connection refused")

        async def ping(self):
            raise RedisConnectionError("connection refused")

    gate = RedisGate(DownRedis())
    with pytest.raises(GateUnavailable):
        await gate.claim(KEY)
    assert await gate.ping() is False


# --- The endpoint -----------------------------------------------------------------------------------


class SlowSpecialists(FakeSpecialists):
    """Takes a moment to answer, so simultaneous requests really overlap."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ownership_checks = 0

    async def owns_transaction(self, caller, transaction_id):
        self.ownership_checks += 1
        return await super().owns_transaction(caller, transaction_id)

    async def reconcile_ledger(self, dispute, token, traceparent=None):
        await asyncio.sleep(0.05)
        return await super().reconcile_ledger(dispute, token, traceparent)


class Supervisor:
    """A supervisor app with scripted dependencies, driven by an async HTTP client."""

    def __init__(self, gate=None, specialists=None, script=HAPPY):
        self.model = ScriptedChatModel(script=script)
        self.specialists = specialists or SlowSpecialists()
        self.app = create_app(auth=SETTINGS, model=self.model, specialists=self.specialists, tracing=Tracing(),
                              policy=RefundPolicyConfig(), gate=gate or InMemoryGate())

    async def __aenter__(self):
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://supervisor")
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()
        await self._lifespan.__aexit__(*exc)

    async def post(self, user="user-1001", **changes):
        return await self.http.post(URL, json=DISPUTE | changes, headers=bearer(user))


@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_ten_taps_run_one_triage(backend):
    async with Supervisor(gate=gates()[backend]) as supervisor:
        responses = await asyncio.gather(*(supervisor.post() for _ in range(10)))

        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200] + [409] * 9
        assert len(supervisor.model.seen) == len(HAPPY)  # the models ran for one request only
        assert supervisor.specialists.ledger_calls == 1 and supervisor.specialists.ownership_checks == 1
        rejected = next(r for r in responses if r.status_code == 409)
        assert rejected.json() == {"detail": "this dispute is already being processed"}
        assert rejected.headers["retry-after"] == "5"


async def test_a_later_duplicate_gets_the_same_result_without_running_again():
    async with Supervisor() as supervisor:
        first = await supervisor.post()
        again = await supervisor.post(description="WHY IS NOBODY ANSWERING", reason="duplicate_charge")

        assert (first.status_code, again.status_code) == (200, 200)
        assert again.json() == first.json()  # the same dispute, same dispute_id
        assert again.headers["idempotent-replay"] == "true" and "idempotent-replay" not in first.headers
        assert len(supervisor.model.seen) == len(HAPPY)


async def test_another_customer_is_not_a_duplicate():
    async with Supervisor(script=HAPPY + HAPPY) as supervisor:
        first, second = await supervisor.post("user-1001"), await supervisor.post("user-1002")
        assert first.json()["dispute_id"] != second.json()["dispute_id"]
        assert "idempotent-replay" not in second.headers


async def test_a_refused_request_does_not_hold_the_key():
    async with Supervisor(specialists=SlowSpecialists(owns=False)) as supervisor:
        assert (await supervisor.post()).status_code == 404
        assert (await supervisor.post()).status_code == 404  # judged again, not answered "in progress"
        assert supervisor.specialists.ownership_checks == 2 and supervisor.model.seen == []


async def test_unreachable_store_fails_closed_before_any_work():
    class DownGate(InMemoryGate):
        async def claim(self, key):
            raise GateUnavailable("ConnectionError")

    async with Supervisor(gate=DownGate()) as supervisor:
        response = await supervisor.post()
        assert response.status_code == 503 and response.headers["retry-after"] == "10"
        assert supervisor.model.seen == [] and supervisor.specialists.ownership_checks == 0


async def test_result_is_returned_even_if_it_cannot_be_stored():
    class ForgetfulGate(InMemoryGate):
        async def complete(self, key, result):
            raise GateUnavailable("ConnectionError")

    async with Supervisor(gate=ForgetfulGate()) as supervisor:
        response = await supervisor.post()
        assert response.status_code == 200 and response.json()["status"] == "refund_approved"


async def test_readiness_depends_on_the_store():
    class DownGate(InMemoryGate):
        async def ping(self):
            return False

    async with Supervisor(gate=DownGate()) as supervisor:
        assert (await supervisor.http.get("/readyz")).status_code == 503


async def test_stored_result_is_the_full_triage_result():
    gate = InMemoryGate()
    async with Supervisor(gate=gate) as supervisor:
        first = await supervisor.post()
        stored = json.loads((await gate.claim(dispute_key("user-1001", DISPUTE["transaction_id"]))).result)
        assert stored == first.json()
