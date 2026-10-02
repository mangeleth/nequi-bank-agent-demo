"""The judge in the background (ADR-0026): the triage queues a job, the judge worker grades the
explanation against the records and stores the verdict, and nothing it does affects the dispute."""

import asyncio
from uuid import uuid4

import fakeredis
import pytest

from services.judge.rubric import PROMPT_VERSION
from services.judge.worker import JudgeWorker, case_for
from services.supervisor import judge_jobs as jobs_module
from services.supervisor.judge_jobs import InMemoryJudgeJobs, RedisJudgeJobs
from services.supervisor.store import InMemoryDisputeStore
from shared.schemas import DisputeRequest, DisputeStatus, TriageResult
from tests.fakes import ScriptedChatModel, ai
from tests.fakes import tool_call as call

TX = "TX-20261001000001"
EVIDENCE = {"transaction": {"transaction_id": TX, "failure_code": "PROCESSING_ERROR", "debited_amount": "50000.00"},
            "risk_signals": {"engine_score": 0.08}}


class FakeEvidence:
    def __init__(self, fail_times: int = 0):
        self.calls, self.fail_times = 0, fail_times

    async def for_dispute(self, user_id, transaction_id, written_at=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("core systems unavailable")
        return EVIDENCE


def verdict_call(grounded=True):
    return ai(call("JudgeOutput", "j", groundedness={"passed": grounded, "reason": "checked"},
                   completeness={"passed": True, "reason": "ok"}, clarity={"passed": True, "reason": "ok"}))


async def finished_dispute(store, *, explanation="It failed with a processing error.", incident=None):
    dispute_id = uuid4()
    request = DisputeRequest(transaction_id=TX, reason="failed_transfer", claimed_amount="50000.00")
    await store.create(dispute_id=dispute_id, dispute_key=f"k:{dispute_id}", user_id="user-1001",
                       request=request, customer_message="received")
    await store.start(dispute_id, "checking")
    result = {"dispute_id": dispute_id, "transaction_id": TX, "status": DisputeStatus.CLOSED_NO_REFUND,
              "customer_message": "m", "steps": []}
    if explanation:
        result["verdict"] = {"dispute_id": dispute_id, "transaction_id": TX, "decision": "no_action",
                             "explanation": explanation, "decided_at": "2026-10-02T12:00:00Z"}
    if incident:
        result["incident"] = incident
    await store.finish(dispute_id, TriageResult.model_validate(result))
    return dispute_id


async def drain(worker: JudgeWorker, jobs: InMemoryJudgeJobs) -> None:
    task = asyncio.create_task(worker.run_forever())
    for _ in range(200):
        if jobs.pending() == 0:
            break
        await asyncio.sleep(0.01)
    task.cancel()


async def test_the_worker_grades_and_stores_the_verdict():
    store, jobs = InMemoryDisputeStore(), InMemoryJudgeJobs()
    dispute_id = await finished_dispute(store)
    await jobs.enqueue(dispute_id)
    model = ScriptedChatModel(script=[verdict_call(grounded=False)])

    await drain(JudgeWorker(store=store, jobs=jobs, model=model, evidence=FakeEvidence()), jobs)

    stored = await store.judgement(dispute_id)
    assert (stored["passed"], stored["prompt_version"]) == (False, PROMPT_VERSION)
    assert (await store.load(dispute_id)).business_status == DisputeStatus.CLOSED_NO_REFUND  # untouched
    shown = model.everything_shown_to_model()
    assert "PROCESSING_ERROR" in shown and "It failed with a processing error." in shown  # evidence and answer


async def test_a_failing_job_is_retried_then_recorded_as_not_judged():
    store, jobs = InMemoryDisputeStore(), InMemoryJudgeJobs()
    dispute_id = await finished_dispute(store)
    await jobs.enqueue(dispute_id)
    worker = JudgeWorker(store=store, jobs=jobs, model=ScriptedChatModel(script=[verdict_call()]),
                         evidence=FakeEvidence(fail_times=99))
    task = asyncio.create_task(worker.run_forever())
    for _ in range(jobs_module.MAX_ATTEMPTS):
        await asyncio.sleep(0.02)
        await jobs.reclaim()  # what Redis does after the job sat unacknowledged
    await asyncio.sleep(0.02)
    task.cancel()

    stored = await store.judgement(dispute_id)
    assert stored["passed"] is None and "core systems unavailable" in stored["result"]["error"]
    assert jobs.pending() == 0


async def test_nothing_is_judged_when_no_model_wrote_anything():
    store = InMemoryDisputeStore()
    no_text = await finished_dispute(store, explanation=None)
    by_incident = await finished_dispute(store, incident={"incident_id": "INC-20261001-01", "title": "t",
                                                          "confirmed_by": "ops"})
    assert case_for(await store.load(no_text), EVIDENCE) is None
    assert case_for(await store.load(by_incident), EVIDENCE) is None


def test_the_triage_queues_a_judge_job_only_for_model_written_explanations():
    from tests.test_supervisor import HAPPY, covered, triage

    jobs = InMemoryJudgeJobs()
    outcome, _, _ = triage(HAPPY, judge_jobs=jobs)
    assert outcome.view["status"] == "refund_paid" and jobs.pending() == 1

    jobs = InMemoryJudgeJobs()
    triage([], covered(), judge_jobs=jobs)  # decided by an incident: no model, nothing to judge
    assert jobs.pending() == 0


def test_a_broken_judge_queue_never_affects_the_dispute():
    from tests.test_supervisor import HAPPY, triage

    class Broken(InMemoryJudgeJobs):
        async def enqueue(self, dispute_id):
            raise ConnectionError("redis is down")

    outcome, _, _ = triage(HAPPY, judge_jobs=Broken())
    assert outcome.view["status"] == "refund_paid"


# --- The Redis stream, against an in-memory Redis -------------------------------------------------


async def first(jobs: RedisJudgeJobs):
    return await anext(jobs.receive().__aiter__())


async def test_redis_jobs_are_delivered_acknowledged_and_reclaimed(monkeypatch):
    server = fakeredis.FakeServer()
    worker_a = RedisJudgeJobs(fakeredis.FakeAsyncRedis(server=server, decode_responses=True), consumer="a", block_ms=50)
    worker_b = RedisJudgeJobs(fakeredis.FakeAsyncRedis(server=server, decode_responses=True), consumer="b", block_ms=50)
    dispute_id = uuid4()
    await worker_a.enqueue(dispute_id)

    job = await asyncio.wait_for(first(worker_a), 5)  # worker a takes it... and dies without acking
    assert (job.dispute_id, job.attempt) == (dispute_id, 1)

    monkeypatch.setattr(jobs_module, "RECLAIM_IDLE_MS", 0)  # instead of waiting a minute
    again = await asyncio.wait_for(first(worker_b), 5)  # worker b reclaims it
    assert (again.dispute_id, again.job_id, again.attempt) == (dispute_id, job.job_id, 2)

    await worker_b.ack(again)
    # Acknowledged: nothing is pending any more. (Asked directly: fakeredis blocks the thread on a
    # blocking read, so "wait and expect nothing" would hang the test, not fail it.)
    pending = await fakeredis.FakeAsyncRedis(server=server, decode_responses=True).xpending(
        jobs_module.STREAM, jobs_module.GROUP)
    assert pending["pending"] == 0


async def test_a_malformed_redis_job_is_dropped():
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    jobs = RedisJudgeJobs(client, consumer="a", block_ms=50)
    await client.xgroup_create(jobs_module.STREAM, jobs_module.GROUP, id="0", mkstream=True)
    await client.xadd(jobs_module.STREAM, {"dispute_id": "not-a-uuid"})
    good = uuid4()
    await jobs.enqueue(good)
    assert (await asyncio.wait_for(first(jobs), 5)).dispute_id == good



# --- Evidence as of when the explanation was written (found on the cluster) ----------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

from services.judge.worker import as_of  # noqa: E402

WRITTEN = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)
NOW_REVERSED = {"transaction_id": TX, "settlement_status": "reversed", "debited_amount": "50000.00",
                "credited_amount": "50000.00", "failure_code": "PROCESSING_ERROR"}


def refund(paid_at):
    return {"refund_id": "RF-1", "amount": "50000.00", "executed_at": paid_at.isoformat()}


def test_a_refund_paid_after_the_explanation_is_taken_out_of_the_evidence():
    before, moment = as_of(NOW_REVERSED, refund(WRITTEN + timedelta(seconds=2)), WRITTEN)
    assert (before["settlement_status"], before["credited_amount"]) == ("failed", "0.00")
    assert "when the explanation was written" in moment
    assert "RF-1" not in moment and "refund" not in moment  # nothing about what happened later


@pytest.mark.parametrize(("paid", "written"), [
    (WRITTEN - timedelta(days=1), WRITTEN),  # refunded BEFORE: part of what the agents saw
    (None, WRITTEN),  # never refunded
    (WRITTEN + timedelta(seconds=2), None),  # unknown when it was written: no guessing
])
def test_otherwise_the_current_records_are_the_evidence(paid, written):
    current, _ = as_of(NOW_REVERSED, refund(paid) if paid else None, written)
    assert current == NOW_REVERSED
