"""Judge worker (ADR-0026): grades each finished dispute's explanation in the background.

    take a job from the "judge-jobs" stream
    load the dispute; build the case: the customer's question, what the models wrote, and the
        evidence, read by code from Core Systems (the transaction and its risk signals)
    ask the judge; save its verdict next to the dispute; acknowledge the job

It runs after the customer has their answer and never changes a decision. A job that fails is
left unacknowledged and is retried (another worker can reclaim it); after its last attempt the
dispute is recorded as "could not be judged".

Its identity may call the model and read the database password; nothing else. It has no Service:
no request can reach it. Run locally:  uvicorn services.judge.worker:create_app --factory --port 8007
"""

import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel

from services.judge.judge import judge
from services.judge.rubric import PROMPT_VERSION, JudgeCase
from services.supervisor.judge_jobs import JudgeJob, JudgeJobs, build_judge_jobs
from services.supervisor.store import DisputeRecord, DisputeStore, open_store

log = logging.getLogger("judge")


def as_of(transaction: dict, refund: dict | None, written_at: datetime | None) -> tuple[dict, str]:
    """The transaction as it was when the explanation was written.

    The judge runs after the decision, and often after the refund was paid: the ledger then shows
    `reversed`, and a correct explanation ("it failed, nothing arrived") would look wrong. The
    explanation is judged against the records it was written from, so a refund paid AFTER it was
    written is taken back out, by code. (Found on the cluster: the first real verdict failed a
    correct explanation for exactly this reason.)
    """
    if not refund or written_at is None:
        return transaction, "current records"
    paid_at = datetime.fromisoformat(str(refund["executed_at"]).replace("Z", "+00:00"))
    if paid_at <= written_at:
        return transaction, "current records (the refund was paid before the explanation was written)"
    amount = Decimal(str(refund["amount"]))
    before = transaction | {
        "settlement_status": "failed",
        "credited_amount": str(Decimal(str(transaction["credited_amount"])) - amount),
    }
    # Say only which moment this is. Mentioning the later refund leaks the future into the evidence:
    # the judge then reads "refund recommended" as contradicting "already paid" (seen on the cluster).
    return before, f"the records as of {written_at.isoformat()}, when the explanation was written"


class Evidence:
    """The records, read by code from Core Systems: never the agents' own words."""

    def __init__(self, http: httpx.AsyncClient, core_url: str) -> None:
        self._http, self._core = http, core_url.rstrip("/")

    async def for_dispute(self, user_id: str, transaction_id: str, written_at: datetime | None = None) -> dict:
        headers = {"X-Customer-Id": user_id}
        base = f"{self._core}/v1"
        tx = await self._http.get(f"{base}/core-banking/transactions/{transaction_id}", headers=headers)
        risk = await self._http.get(f"{base}/risk/transactions/{transaction_id}/signals", headers=headers)
        refund = await self._http.get(f"{base}/core-banking/transactions/{transaction_id}/refund", headers=headers)
        tx.raise_for_status()
        risk.raise_for_status()
        if refund.status_code not in (200, 404):
            refund.raise_for_status()
        transaction, moment = as_of(tx.json(), refund.json() if refund.status_code == 200 else None, written_at)
        return {"records_describe": moment, "transaction": transaction, "risk_signals": risk.json()}


def written_at(record: DisputeRecord) -> datetime | None:
    decided = ((record.result or {}).get("verdict") or {}).get("decided_at")
    return datetime.fromisoformat(str(decided).replace("Z", "+00:00")) if decided else None


def case_for(record: DisputeRecord, evidence: dict) -> JudgeCase | None:
    """What to judge: the parts of the result a model wrote. None if no model wrote anything."""
    result = record.result or {}
    answer = {}
    if explanation := (result.get("verdict") or {}).get("explanation"):
        answer["explanation"] = explanation
    if summary := (result.get("ledger") or {}).get("summary"):
        answer["ledger_summary"] = summary
    if rationale := (result.get("fraud") or {}).get("rationale"):
        answer["fraud_rationale"] = rationale
    if not answer or result.get("incident"):
        return None
    question = {k: record.request.get(k) for k in ("reason", "transaction_id", "claimed_amount", "description")}
    return JudgeCase(question=question, answer=answer, evidence=evidence)


class JudgeWorker:
    def __init__(self, *, store: DisputeStore, jobs: JudgeJobs, model: BaseChatModel, evidence: Evidence) -> None:
        self.store, self.jobs, self.model, self.evidence = store, jobs, model, evidence

    async def handle(self, job: JudgeJob) -> None:
        """Grade one dispute. Never raises: every outcome acknowledges the job, or leaves it for a retry."""
        try:
            record = await self.store.load(job.dispute_id)
            existing = await self.store.judgement(job.dispute_id) if record else None
            if record is None or (existing and existing.get("prompt_version") == PROMPT_VERSION
                                  and existing.get("passed") is not None):
                await self.jobs.ack(job)  # gone, or already judged with this prompt: nothing to do
                return
            evidence = await self.evidence.for_dispute(record.user_id, record.transaction_id, written_at(record))
            case = case_for(record, evidence)
            if case is None:
                await self.jobs.ack(job)
                return
            verdict = await judge(self.model, case)
            await self.store.save_judgement(job.dispute_id, verdict.model_dump(), passed=verdict.passed,
                                            prompt_version=PROMPT_VERSION)
            await self.jobs.ack(job)
            log.info("judged dispute %s: %s", job.dispute_id, "pass" if verdict.passed else "FAIL")
        except Exception as exc:
            log.warning("judging dispute %s failed (attempt %d): %s", job.dispute_id, job.attempt, exc)
            if job.is_last:
                with suppress(Exception):
                    await self.store.save_judgement(job.dispute_id, {"error": f"{type(exc).__name__}: {exc}"[:300]},
                                                    passed=None, prompt_version=PROMPT_VERSION)
                    await self.jobs.ack(job)
            # Otherwise not acknowledged: the job is retried when it is reclaimed.

    async def run_forever(self) -> None:
        async for job in self.jobs.receive():
            await self.handle(job)


def create_app(*, store: DisputeStore | None = None, jobs: JudgeJobs | None = None,
               model: BaseChatModel | None = None, evidence: Evidence | None = None) -> FastAPI:
    """Build the worker. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = httpx.AsyncClient(timeout=20) if evidence is None else None
        app.state.store, pool = (store, None) if store is not None else await open_store()
        app.state.jobs = jobs or build_judge_jobs()
        if app.state.jobs is None:
            raise ValueError("JUDGE_JOBS must be redis or memory for the judge worker")
        if model is None:
            from shared.llm import build_chat_model
        worker = JudgeWorker(store=app.state.store, jobs=app.state.jobs, model=model or build_chat_model(),
                             evidence=evidence or Evidence(http, os.environ["CORE_SYSTEMS_URL"]))
        app.state.consumer = asyncio.create_task(worker.run_forever())
        log.info("judge worker started (prompt %s)", PROMPT_VERSION)
        yield
        app.state.consumer.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.consumer
        if pool is not None:
            await pool.close()
        if http is not None:
            await http.aclose()

    app = FastAPI(title="Judge Worker", version="1.0.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        consuming = not request.app.state.consumer.done()
        return JSONResponse({"status": "ok" if consuming else "consumer stopped"}, status_code=200 if consuming else 503)

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        ready = await request.app.state.store.ping() and await request.app.state.jobs.ping()
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
