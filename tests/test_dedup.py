"""Deduplication at the gate: one dispute per customer and transaction, however many times the
customer taps. The same behaviour is required of the in-memory store and the Redis store."""

import asyncio

import fakeredis
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from services.supervisor.dedup import GateSettings, GateUnavailable, InMemoryGate, RedisGate, dispute_key
from services.supervisor.main import create_app
from services.supervisor.store import InMemoryDisputeStore
from shared.refund_policy import RefundPolicyConfig
from shared.tracing import Tracing
from tests.fakes import ScriptedChatModel
from tests.jwt_helpers import SETTINGS, bearer
from tests.test_supervisor import DISPUTE, HAPPY, URL, FakeSpecialists

KEY = dispute_key("user-1001", "TX-20261001000001")
DISPUTE_ID = "7d0c3c1e-0c58-4c0c-9a1e-2f0f6b1a9a11"


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


async def test_after_completion_duplicates_are_pointed_to_the_dispute(gate):
    await gate.claim(KEY)
    await gate.complete(KEY, DISPUTE_ID)
    duplicate = await gate.claim(KEY)
    assert (duplicate.claimed, duplicate.in_progress, duplicate.dispute_id) == (False, False, DISPUTE_ID)


async def test_release_lets_the_customer_try_again(gate):
    await gate.claim(KEY)
    await gate.release(KEY)
    assert (await gate.claim(KEY)).claimed


async def test_release_never_forgets_an_existing_dispute(gate):
    await gate.claim(KEY)
    await gate.complete(KEY, DISPUTE_ID)
    await gate.release(KEY)
    assert (await gate.claim(KEY)).dispute_id == DISPUTE_ID


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
        await asyncio.sleep(0.02)
        return await super().owns_transaction(caller, transaction_id)

    async def reconcile_ledger(self, dispute, token, traceparent=None):
        await asyncio.sleep(0.05)
        return await super().reconcile_ledger(dispute, token, traceparent)


class Supervisor:
    """A supervisor app with scripted dependencies, driven by an async HTTP client."""

    def __init__(self, gate=None, store=None, specialists=None, script=HAPPY):
        self.model = ScriptedChatModel(script=script)
        self.specialists = specialists or SlowSpecialists()
        self.gate = gate or InMemoryGate()
        self.app = create_app(auth=SETTINGS, model=self.model, specialists=self.specialists, tracing=Tracing(),
                              policy=RefundPolicyConfig(), gate=self.gate, store=store or InMemoryDisputeStore())

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

    async def get(self, dispute_id, user="user-1001"):
        return await self.http.get(f"{URL}/{dispute_id}", headers=bearer(user))

    async def finished(self, dispute_id, user="user-1001") -> dict:
        for _ in range(500):
            view = (await self.get(dispute_id, user)).json()
            if view["execution_status"] in ("finished", "failed"):
                return view
            await asyncio.sleep(0.01)
        raise AssertionError(f"dispute never finished: {view}")


@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_ten_taps_create_one_dispute_and_run_one_triage(backend):
    async with Supervisor(gate=gates()[backend]) as supervisor:
        responses = await asyncio.gather(*(supervisor.post() for _ in range(10)))

        accepted = [r for r in responses if r.status_code == 202]
        assert len(accepted) == 1
        dispute_id = accepted[0].json()["dispute_id"]
        for duplicate in (r for r in responses if r.status_code != 202):
            if duplicate.status_code == 409:  # arrived while the first tap was still being accepted
                assert duplicate.json() == {"detail": "this dispute is already being processed"}
                assert duplicate.headers["retry-after"] == "5"
            else:  # arrived after: pointed to the same dispute
                assert duplicate.status_code == 200 and duplicate.headers["idempotent-replay"] == "true"
                assert duplicate.json()["dispute_id"] == dispute_id

        await supervisor.finished(dispute_id)
        assert len(supervisor.model.seen) == len(HAPPY)  # the models ran once
        assert supervisor.specialists.ledger_calls == 1 and supervisor.specialists.ownership_checks == 1


async def test_submission_is_accepted_at_once_and_progress_is_readable():
    async with Supervisor() as supervisor:
        accepted = await supervisor.post()
        body = accepted.json()

        assert accepted.status_code == 202
        assert accepted.headers["location"] == f"/v1/disputes/{body['dispute_id']}"
        assert (body["status"], body["execution_status"], body["result"]) == ("received", "queued", None)
        assert body["customer_message"] == "We've received your dispute."

        during = (await supervisor.get(body["dispute_id"])).json()  # the ledger lookup is still running
        assert (during["status"], during["execution_status"]) == ("investigating", "running")
        assert during["customer_message"] == "We're checking the records for this transfer."

        done = await supervisor.finished(body["dispute_id"])
        assert (done["status"], done["execution_status"]) == ("refund_approved", "finished")
        assert done["result"]["approval"]["route"] == "auto_approved"


async def test_a_later_duplicate_is_pointed_to_the_same_dispute_without_running_again():
    async with Supervisor() as supervisor:
        first = (await supervisor.post()).json()
        await supervisor.finished(first["dispute_id"])
        again = await supervisor.post(description="WHY IS NOBODY ANSWERING", reason="duplicate_charge")

        assert again.status_code == 200 and again.headers["idempotent-replay"] == "true"
        assert again.json()["dispute_id"] == first["dispute_id"]
        assert again.json()["status"] == "refund_approved"  # the dispute as it stands now
        assert len(supervisor.model.seen) == len(HAPPY)


async def test_the_database_catches_a_duplicate_that_redis_forgot():
    async with Supervisor() as supervisor:
        first = (await supervisor.post()).json()
        await supervisor.finished(first["dispute_id"])
        supervisor.gate._entries.clear()  # Redis restarted and lost every key

        again = await supervisor.post()
        assert again.status_code == 200 and again.headers["idempotent-replay"] == "true"
        assert again.json()["dispute_id"] == first["dispute_id"]
        assert len(supervisor.model.seen) == len(HAPPY)  # no second triage
        assert (await supervisor.gate.claim(dispute_key("user-1001", DISPUTE["transaction_id"]))).dispute_id == first["dispute_id"]


async def test_another_customer_is_not_a_duplicate():
    async with Supervisor(script=HAPPY + HAPPY) as supervisor:
        first, second = await supervisor.post("user-1001"), await supervisor.post("user-1002")
        assert (first.status_code, second.status_code) == (202, 202)
        assert first.json()["dispute_id"] != second.json()["dispute_id"]


async def test_a_dispute_is_readable_only_by_its_owner():
    async with Supervisor() as supervisor:
        dispute_id = (await supervisor.post("user-1001")).json()["dispute_id"]
        assert (await supervisor.get(dispute_id, "user-1001")).status_code == 200
        assert (await supervisor.get(dispute_id, "user-1002")).status_code == 404
        assert (await supervisor.get("7d0c3c1e-0c58-4c0c-9a1e-2f0f6b1a9a11")).status_code == 404
        assert (await supervisor.http.get(f"{URL}/{dispute_id}")).status_code == 401
        await supervisor.finished(dispute_id)


async def test_a_refused_request_stores_nothing_and_does_not_hold_the_key():
    async with Supervisor(specialists=SlowSpecialists(owns=False)) as supervisor:
        assert (await supervisor.post()).status_code == 404
        assert (await supervisor.post()).status_code == 404  # judged again, not answered "in progress"
        assert supervisor.specialists.ownership_checks == 2 and supervisor.model.seen == []


async def test_unreachable_gate_fails_closed_before_any_work():
    class DownGate(InMemoryGate):
        async def claim(self, key):
            raise GateUnavailable("ConnectionError")

    async with Supervisor(gate=DownGate()) as supervisor:
        response = await supervisor.post()
        assert response.status_code == 503 and response.headers["retry-after"] == "10"
        assert supervisor.model.seen == [] and supervisor.specialists.ownership_checks == 0


async def test_unreachable_database_refuses_the_dispute_and_frees_the_key():
    class DownStore(InMemoryDisputeStore):
        async def create(self, **kwargs):
            raise ConnectionError("database is down")

    async with Supervisor(store=DownStore()) as supervisor:
        assert (await supervisor.post()).status_code == 503
        assert (await supervisor.post()).status_code == 503  # the key was given back: not a 409
        assert supervisor.model.seen == []


async def test_dispute_is_accepted_even_if_the_gate_cannot_record_it():
    class ForgetfulGate(InMemoryGate):
        async def complete(self, key, dispute_id):
            raise GateUnavailable("ConnectionError")

    async with Supervisor(gate=ForgetfulGate()) as supervisor:
        accepted = await supervisor.post()
        assert accepted.status_code == 202  # the database guards the key from here on
        await supervisor.finished(accepted.json()["dispute_id"])


async def test_a_run_that_breaks_outside_the_graph_leaves_the_dispute_with_a_person():
    class BrokenStore(InMemoryDisputeStore):
        async def finish(self, dispute_id, result):
            raise ConnectionError("database went away while saving the result")

    async with Supervisor(store=BrokenStore()) as supervisor:
        dispute_id = (await supervisor.post()).json()["dispute_id"]
        view = await supervisor.finished(dispute_id)
        assert (view["execution_status"], view["status"]) == ("failed", "pending_human_approval")
        assert "marked for review by a person" in view["customer_message"]  # never "failed" to the customer


@pytest.mark.parametrize("down", ["gate", "store"])
async def test_readiness_depends_on_the_gate_and_the_database(down):
    class DownGate(InMemoryGate):
        async def ping(self):
            return False

    class DownStore(InMemoryDisputeStore):
        async def ping(self):
            return False

    options = {"gate": DownGate()} if down == "gate" else {"store": DownStore()}
    async with Supervisor(**options) as supervisor:
        assert (await supervisor.http.get("/readyz")).status_code == 503
