"""The refund payer (ADR-0021): approved refunds are paid from their own queue, at a controlled
pace, can be paused, are never paid twice, and end with a person when the ledger stays silent."""

import asyncio
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.supervisor.payments import REFUND_MAX_DELIVERIES, Pace, RefundPayer
from services.supervisor.queue import InMemoryQueue
from services.supervisor.store import InMemoryDisputeStore
from shared.schemas import (
    ApprovalRoute,
    DisputeRequest,
    DisputeStatus,
    LedgerReconciliation,
    PolicyCheck,
    RefundApproval,
    TriageResult,
)
from tests.test_supervisor import TX, FakeSpecialists

REQUEST = DisputeRequest(transaction_id=TX, reason="failed_transfer", claimed_amount="50000.00")


async def approved_dispute(store: InMemoryDisputeStore):
    """A dispute whose triage finished with an automatic approval, as the worker leaves it."""
    dispute_id = uuid4()
    await store.create(dispute_id=dispute_id, dispute_key=f"dispute:{dispute_id}", user_id="user-1001",
                       request=REQUEST, customer_message="received")
    await store.start(dispute_id, "checking")
    approval = RefundApproval(
        dispute_id=dispute_id, transaction_id=TX, route=ApprovalRoute.AUTO_APPROVED, approved_amount="50000.00",
        checks=[PolicyCheck(name="amount_matches_ledger", passed=True, detail="ok")],
        policy_version="test", evaluated_at=datetime.now(UTC))
    ledger = LedgerReconciliation(transaction_id=TX, settlement_status="failed", debited_amount="50000.00",
                                  credited_amount="0.00", summary="From the ledger.")
    await store.finish(dispute_id, TriageResult(
        dispute_id=dispute_id, transaction_id=TX, status=DisputeStatus.REFUND_APPROVED, approval=approval,
        ledger=ledger, customer_message="A refund of 50000.00 COP has been approved. It has not been paid yet."))
    return dispute_id


async def run_payer(payer: RefundPayer, until, timeout: float = 5.0) -> None:
    task = asyncio.create_task(payer.run_forever())
    try:
        deadline = time.monotonic() + timeout
        while not await until():
            if time.monotonic() > deadline:
                raise AssertionError("the payer did not get there in time")
            await asyncio.sleep(0.01)
    finally:
        task.cancel()


def setup(**specialists):
    store, refunds = InMemoryDisputeStore(), InMemoryQueue(max_deliveries=REFUND_MAX_DELIVERIES)
    fake = FakeSpecialists(**specialists)
    return store, refunds, fake


async def status(store, dispute_id) -> DisputeStatus:
    return (await store.load(dispute_id)).business_status


async def test_an_approved_refund_is_paid_from_the_queue():
    store, refunds, ledger = setup()
    dispute_id = await approved_dispute(store)
    await refunds.send(dispute_id)

    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, per_second=100, retry_delay_seconds=0)
    await run_payer(payer, lambda: _is(store, dispute_id, DisputeStatus.REFUND_PAID))

    assert [call[3] for call in ledger.pay_calls] == [f"dispute:{dispute_id}"]


async def test_a_duplicate_message_pays_nothing_more():
    store, refunds, ledger = setup()
    dispute_id = await approved_dispute(store)
    for _ in range(3):  # e.g. the worker queued it again after a redelivery
        await refunds.send(dispute_id)

    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, per_second=100, retry_delay_seconds=0)
    await run_payer(payer, lambda: _drained(refunds))

    assert len(ledger.pay_calls) == 1  # the later messages found it already paid
    assert await status(store, dispute_id) == DisputeStatus.REFUND_PAID


async def test_payments_start_no_faster_than_the_pace():
    store, refunds, ledger = setup()
    ids = [await approved_dispute(store) for _ in range(5)]
    for dispute_id in ids:
        await refunds.send(dispute_id)

    started = time.monotonic()
    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, per_second=20, retry_delay_seconds=0)
    await run_payer(payer, lambda: _all_paid(store, ids))

    # 5 payments at 20 per second: the 5th cannot start before 4 intervals of 50 ms have passed.
    assert time.monotonic() - started >= 4 * (1 / 20) * 0.9
    assert len(ledger.pay_calls) == 5


async def test_pace_spaces_out_a_burst():
    pace, stamps = Pace(per_second=50), []
    for _ in range(6):
        await pace.wait()
        stamps.append(time.monotonic())
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:], strict=False)]
    assert min(gaps) >= (1 / 50) * 0.8


async def test_paused_payments_wait_in_the_queue():
    store, refunds, ledger = setup()
    dispute_id = await approved_dispute(store)
    await refunds.send(dispute_id)

    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, paused=True, retry_delay_seconds=0)
    task = asyncio.create_task(payer.run_forever())
    await asyncio.sleep(0.2)
    task.cancel()

    assert ledger.pay_calls == []
    assert await status(store, dispute_id) == DisputeStatus.REFUND_APPROVED  # still approved, still queued
    assert refunds._ready.qsize() == 1

    # Unpaused, the same message is paid.
    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, per_second=100, retry_delay_seconds=0)
    await run_payer(payer, lambda: _is(store, dispute_id, DisputeStatus.REFUND_PAID))


async def test_a_refusal_goes_to_a_person_without_retrying():
    store, refunds, ledger = setup(pay="refused")
    dispute_id = await approved_dispute(store)
    await refunds.send(dispute_id)

    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, per_second=100, retry_delay_seconds=0)
    await run_payer(payer, lambda: _is(store, dispute_id, DisputeStatus.PENDING_HUMAN_APPROVAL))

    assert len(ledger.pay_calls) == 1 and refunds.dead_letters == []
    assert "amount_mismatch" in (await store.load(dispute_id)).result["escalation_reason"]


async def test_a_silent_ledger_is_tried_five_times_then_a_person_and_the_dead_letter_queue():
    store, refunds, ledger = setup(pay="down")
    dispute_id = await approved_dispute(store)
    await refunds.send(dispute_id)

    payer = RefundPayer(store=store, queue=refunds, specialists=ledger, per_second=100, retry_delay_seconds=0)
    await run_payer(payer, lambda: _is(store, dispute_id, DisputeStatus.PENDING_HUMAN_APPROVAL))
    await asyncio.sleep(0.05)

    assert len(ledger.pay_calls) == REFUND_MAX_DELIVERIES == 5
    assert len({call[3] for call in ledger.pay_calls}) == 1  # every attempt with the same key
    assert [reason for _, reason in refunds.dead_letters] == ["payment failed on its last delivery"]
    reason = (await store.load(dispute_id)).result["escalation_reason"]
    assert f"dispute:{dispute_id}" in reason  # where a person should look in the ledger


@pytest.mark.parametrize("per_second", [0, -1])
def test_a_pace_must_be_positive(per_second):
    with pytest.raises(ValueError):
        Pace(per_second)


async def _is(store, dispute_id, wanted) -> bool:
    return await status(store, dispute_id) == wanted


async def _drained(refunds: InMemoryQueue) -> bool:
    return refunds._ready.empty() and not refunds._locked


async def _all_paid(store, ids) -> bool:
    return all([await status(store, i) == DisputeStatus.REFUND_PAID for i in ids])


def test_the_code_and_the_queue_agree_on_the_delivery_limit():
    """The broker dead-letters after --max-delivery-count; the payer decides "last delivery" from
    REFUND_MAX_DELIVERIES. If they differ, the last attempt would never send the dispute to a person."""
    import re
    from pathlib import Path

    makefile = Path(__file__).resolve().parent.parent.joinpath("Makefile").read_text()
    refunds_queue = re.search(r"-n \$\(REFUNDS_QUEUE\).*?--max-delivery-count (\d+)", makefile, re.S)
    assert int(refunds_queue.group(1)) == REFUND_MAX_DELIVERIES
