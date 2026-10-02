"""The dispute store's rules, checked against both adapters. The PostgreSQL cases run when
TEST_DATABASE_URL is set (`make test-db` starts a throwaway PostgreSQL and sets it)."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.supervisor.store import InMemoryDisputeStore, PostgresDisputeStore
from shared.schemas import DisputeRequest, DisputeStatus, ExecutionStatus, TriageResult

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
REQUEST = DisputeRequest(transaction_id="TX-20261001000001", reason="failed_transfer", claimed_amount="50000.00")


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.skipif(
    not DATABASE_URL, reason="needs a PostgreSQL: run `make test-db`"))])
async def store(request):
    if request.param == "memory":
        yield InMemoryDisputeStore()
        return
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row})
    await pool.open(wait=True, timeout=30)
    store = PostgresDisputeStore(pool)
    await store.migrate()
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE disputes CASCADE")
    yield store
    await pool.close()


async def new_dispute(store, key=None, user="user-1001"):
    return await store.create(dispute_id=uuid4(), dispute_key=key or f"dispute:{uuid4().hex}", user_id=user,
                              request=REQUEST, customer_message="We've received your dispute.")


def result(dispute_id, status=DisputeStatus.REFUND_APPROVED) -> TriageResult:
    return TriageResult(dispute_id=dispute_id, transaction_id=REQUEST.transaction_id, status=status,
                        customer_message="A refund of 50000.00 COP has been approved. It has not been paid yet.",
                        steps=["supervisor -> finish: scripted"])


async def test_a_new_dispute_is_received_and_queued(store):
    record, created = await new_dispute(store)
    assert created
    assert (record.execution_status, record.business_status) == (ExecutionStatus.QUEUED, DisputeStatus.RECEIVED)
    assert record.result is None and record.attempts == 0
    assert record.created_at.tzinfo is not None


async def test_one_dispute_per_key(store):
    first, created_first = await new_dispute(store, key="dispute:same")
    second, created_second = await new_dispute(store, key="dispute:same")
    assert (created_first, created_second) == (True, False)
    assert second.dispute_id == first.dispute_id


async def test_ten_simultaneous_creates_make_one_dispute(store):
    outcomes = await asyncio.gather(*(new_dispute(store, key="dispute:race") for _ in range(10)))
    assert sum(created for _, created in outcomes) == 1
    assert len({record.dispute_id for record, _ in outcomes}) == 1


async def test_reads_are_scoped_to_the_customer(store):
    record, _ = await new_dispute(store, user="user-1001")
    assert (await store.get(record.dispute_id, "user-1001")).dispute_id == record.dispute_id
    assert await store.get(record.dispute_id, "user-1002") is None
    assert await store.get(uuid4(), "user-1001") is None


async def test_lifecycle_keeps_the_two_statuses_separate(store):
    record, _ = await new_dispute(store)
    assert await store.start(record.dispute_id, "We're checking the records for this transfer.")
    running = await store.get(record.dispute_id, "user-1001")
    assert (running.execution_status, running.business_status, running.attempts) == (
        ExecutionStatus.RUNNING, DisputeStatus.INVESTIGATING, 1)

    # The run finishes; the dispute still waits for a person.
    assert await store.finish(record.dispute_id, result(record.dispute_id, DisputeStatus.PENDING_HUMAN_APPROVAL))
    finished = await store.get(record.dispute_id, "user-1001")
    assert (finished.execution_status, finished.business_status) == (
        ExecutionStatus.FINISHED, DisputeStatus.PENDING_HUMAN_APPROVAL)
    assert finished.result["steps"] == ["supervisor -> finish: scripted"]
    assert finished.view().result.status == DisputeStatus.PENDING_HUMAN_APPROVAL


async def test_status_changes_only_from_the_expected_status(store):
    record, _ = await new_dispute(store)
    assert not await store.finish(record.dispute_id, result(record.dispute_id))  # not running yet
    assert await store.start(record.dispute_id, "checking")
    assert not await store.start(record.dispute_id, "checking")  # a second worker cannot start it again
    assert await store.finish(record.dispute_id, result(record.dispute_id))
    assert not await store.finish(record.dispute_id, result(record.dispute_id))  # already finished
    assert not await store.fail(record.dispute_id, "x", "late failure")  # a finished run cannot be failed


async def test_a_failed_run_leaves_the_dispute_with_a_person(store):
    record, _ = await new_dispute(store)
    await store.start(record.dispute_id, "checking")
    assert await store.fail(record.dispute_id, "The case is marked for review by a person.", "unexpected error")
    failed = await store.get(record.dispute_id, "user-1001")
    assert (failed.execution_status, failed.business_status) == (
        ExecutionStatus.FAILED, DisputeStatus.PENDING_HUMAN_APPROVAL)
    assert failed.customer_message == "The case is marked for review by a person."


async def test_runs_that_never_finished_are_failed(store):
    stuck, _ = await new_dispute(store)
    await store.start(stuck.dispute_id, "checking")
    done, _ = await new_dispute(store)
    await store.start(done.dispute_id, "checking")
    await store.finish(done.dispute_id, result(done.dispute_id))

    assert await store.fail_stuck(older_than_seconds=3600, customer_message="x") == 0  # still fresh
    await asyncio.sleep(1.1)
    assert await store.fail_stuck(older_than_seconds=1, customer_message="Marked for review.") == 1
    assert (await store.get(stuck.dispute_id, "user-1001")).execution_status == ExecutionStatus.FAILED
    assert (await store.get(done.dispute_id, "user-1001")).execution_status == ExecutionStatus.FINISHED


async def test_every_change_is_in_the_audit_trail(store):
    record, _ = await new_dispute(store)
    await store.start(record.dispute_id, "checking")
    await store.finish(record.dispute_id, result(record.dispute_id))
    events = await store.events(record.dispute_id)

    assert [(e["execution_status"], e["business_status"], e["note"]) for e in events] == [
        ("queued", "received", "dispute received"),
        ("running", "investigating", "run started"),
        ("finished", "refund_approved", "run finished"),
    ]
    assert all(e["at"] <= datetime.now(UTC) for e in events)


async def test_a_rejected_change_writes_no_event(store):
    record, _ = await new_dispute(store)
    await store.finish(record.dispute_id, result(record.dispute_id))  # refused: not running
    assert len(await store.events(record.dispute_id)) == 1


async def test_ping(store):
    assert await store.ping() is True
