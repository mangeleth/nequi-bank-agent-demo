"""The queue and the worker: what happens to a dispute when a delivery fails, when the worker
dies, when a message is poisonous, and when the queue itself cannot be reached."""

import asyncio
from uuid import UUID, uuid4

import pytest

from services.supervisor.dedup import InMemoryGate
from services.supervisor.queue import MAX_DELIVERIES, Delivery, InMemoryQueue
from services.supervisor.store import InMemoryDisputeStore
from services.supervisor.triage import retry_delay
from shared.schemas import DisputeRequest
from tests.test_dedup import SlowSpecialists, Supervisor
from tests.test_supervisor import HAPPY, route

# The first attempt asks for the ledger and breaks there; the second attempt is a full run.
RETRY_SCRIPT = [route("ledger_agent")] + HAPPY


class FlakySpecialists(SlowSpecialists):
    """The ledger lookup breaks with an unexpected error for the first `broken_calls` calls."""

    def __init__(self, broken_calls: int, **kwargs):
        super().__init__(**kwargs)
        self.broken_calls = broken_calls

    async def reconcile_ledger(self, dispute, token, traceparent=None):
        self.ledger_calls += 1
        if self.ledger_calls <= self.broken_calls:
            raise RuntimeError("connection reset")
        self.ledger_calls -= 1  # the parent counts the successful call
        return await super().reconcile_ledger(dispute, token, traceparent)


async def notes(store, dispute_id: str) -> list[str]:
    return [event["note"] for event in await store.events(UUID(dispute_id))]


# --- Delivery rules of the queue itself --------------------------------------------------------------


def test_one_retry_means_two_deliveries():
    assert MAX_DELIVERIES == 2
    assert not Delivery(uuid4(), 1).is_last and Delivery(uuid4(), 2).is_last


async def test_abandoned_message_is_offered_again_then_dead_lettered():
    queue = InMemoryQueue()
    dispute_id = uuid4()
    await queue.send(dispute_id)
    deliveries = queue.receive()

    first = await deliveries.__anext__()
    await queue.abandon(first)
    second = await deliveries.__anext__()
    assert (first.delivery_count, second.delivery_count) == (1, 2)

    await queue.abandon(second)  # the last delivery failed too
    assert queue.dead_letters == [(dispute_id, "delivery limit reached")]


def test_retry_delay_has_jitter():
    delays = {round(retry_delay(2.0), 6) for _ in range(50)}
    assert len(delays) > 40  # not the same wait every time
    assert all(1.0 <= delay <= 3.0 for delay in delays)  # 50% to 150% of the base


# --- The worker -----------------------------------------------------------------------------------


async def test_the_message_is_only_an_id():
    queue = InMemoryQueue()
    sent = []
    original = queue.send

    async def spy(dispute_id):
        sent.append(dispute_id)
        await original(dispute_id)

    queue.send = spy
    async with Supervisor(queue=queue) as sup:
        dispute_id = (await sup.post()).json()["dispute_id"]
        await sup.finished(dispute_id)
    assert sent == [UUID(dispute_id)]  # no customer, no request text, no token


async def test_a_failed_delivery_is_retried_and_the_dispute_still_finishes():
    store = InMemoryDisputeStore()
    async with Supervisor(store=store, specialists=FlakySpecialists(broken_calls=1), script=RETRY_SCRIPT) as sup:
        dispute_id = (await sup.post()).json()["dispute_id"]
        view = await sup.finished(dispute_id)

    assert (view["execution_status"], view["status"]) == ("finished", "refund_approved")
    assert await notes(store, dispute_id) == [
        "dispute received", "run started", "run restarted after a failed delivery", "run finished"]
    assert (await store.load(UUID(dispute_id))).attempts == 2


async def test_a_poison_message_is_dead_lettered_and_the_dispute_goes_to_a_person():
    queue, store = InMemoryQueue(), InMemoryDisputeStore()
    specialists = FlakySpecialists(broken_calls=99)
    async with Supervisor(queue=queue, store=store, specialists=specialists, script=[route("ledger_agent")]) as sup:
        dispute_id = (await sup.post()).json()["dispute_id"]
        view = await sup.finished(dispute_id)

    assert specialists.ledger_calls == MAX_DELIVERIES  # it could break things twice, not forever
    assert (view["execution_status"], view["status"]) == ("failed", "pending_human_approval")
    assert "marked for review by a person" in view["customer_message"]
    assert queue.dead_letters == [(UUID(dispute_id), "the run failed on its last delivery")]
    assert (await notes(store, dispute_id))[-1] == "the run failed on delivery 2 of 2"


async def test_a_worker_that_dies_mid_run_is_replaced_by_redelivery():
    """The first worker takes the message and vanishes without completing or abandoning it."""
    queue, store = InMemoryQueue(), InMemoryDisputeStore()
    died = asyncio.Event()

    class DiesOnce(SlowSpecialists):
        async def reconcile_ledger(self, dispute, token, traceparent=None):
            if not died.is_set():
                died.set()
                await asyncio.Event().wait()  # hangs forever, like a process that was killed
            return await super().reconcile_ledger(dispute, token, traceparent)

    async with Supervisor(queue=queue, store=store, specialists=DiesOnce(), script=RETRY_SCRIPT) as sup:
        dispute_id = (await sup.post()).json()["dispute_id"]
        await died.wait()
        assert (await sup.get(dispute_id)).json()["execution_status"] == "running"

        await queue.expire_locks()  # what the real queue does when the lock runs out
        view = await sup.finished(dispute_id)

    assert (view["execution_status"], view["status"]) == ("finished", "refund_approved")
    assert "run restarted after a failed delivery" in await notes(store, dispute_id)


async def test_a_duplicate_message_does_not_run_the_dispute_twice():
    queue = InMemoryQueue()
    async with Supervisor(queue=queue) as sup:
        dispute_id = (await sup.post()).json()["dispute_id"]
        await sup.finished(dispute_id)
        calls_after_first_run = len(sup.model.seen)

        await queue.send(UUID(dispute_id))  # the same message arrives again
        await asyncio.sleep(0.1)
        assert len(sup.model.seen) == calls_after_first_run
        assert queue._locked == {}  # the stray message was completed, not left hanging


async def test_a_dispute_that_cannot_be_queued_goes_to_a_person():
    class DownQueue(InMemoryQueue):
        async def send(self, dispute_id):
            raise ConnectionError("queue is down")

    async with Supervisor(queue=DownQueue()) as sup:
        accepted = await sup.post()
        view = accepted.json()

        assert accepted.status_code == 202  # it was stored, so it is not lost
        assert (view["execution_status"], view["status"]) == ("failed", "pending_human_approval")
        assert sup.model.seen == []
        again = await sup.post()  # and the customer cannot create a second one by retrying
        assert again.status_code == 200 and again.json()["dispute_id"] == view["dispute_id"]


async def test_readiness_depends_on_the_queue():
    class DownQueue(InMemoryQueue):
        async def ping(self):
            return False

    async with Supervisor(queue=DownQueue()) as sup:
        assert (await sup.http.get("/readyz")).status_code == 503


async def test_a_run_never_starts_more_often_than_the_delivery_limit():
    store = InMemoryDisputeStore()
    request = DisputeRequest(transaction_id="TX-20261001000001", reason="failed_transfer", claimed_amount="50000.00")
    record, _ = await store.create(dispute_id=uuid4(), dispute_key="dispute:x", user_id="user-1001",
                                   request=request, customer_message="received")
    assert await store.start(record.dispute_id, "checking", max_attempts=2)
    assert await store.start(record.dispute_id, "checking", takeover=True, max_attempts=2)
    assert not await store.start(record.dispute_id, "checking", takeover=True, max_attempts=2)  # a third time: no


@pytest.mark.parametrize("gate", [InMemoryGate])
async def test_happy_path_still_works_through_the_queue(gate):
    async with Supervisor(gate=gate()) as sup:
        dispute_id = (await sup.post()).json()["dispute_id"]
        assert (await sup.finished(dispute_id))["status"] == "refund_approved"
