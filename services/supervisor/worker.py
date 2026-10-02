"""Triage worker: takes disputes from the queue and runs them (ADR-0018).

A separate deployment from the intake API, with its own identity:
    intake API    may SEND to the dispute queue; cannot call a model or sign a token
    this worker   may RECEIVE from the dispute queue, call the model, sign tokens, and SEND approved
                  refunds to the refunds queue; it does not pay
    refund payer  may RECEIVE from the refunds queue and pay (services/supervisor/payer.py)

It exposes only health endpoints, for Kubernetes. Nothing sends it work over HTTP: work arrives
from the queue, so a customer-facing request can never reach this process.

Run locally:  uvicorn services.supervisor.worker:create_app --factory --port 8005
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel

from services.supervisor.clients import HttpSpecialists, Specialists
from services.supervisor.graph import RECURSION_LIMIT, build_graph
from services.supervisor.judge_jobs import JudgeJobs, build_judge_jobs
from services.supervisor.queue import DisputeQueue, ServiceBusQueue
from services.supervisor.store import DisputeStore, open_store
from services.supervisor.triage import RETRY_DELAY_SECONDS, TriageRunner
from shared.delegation import DelegationSettings, TokenSigner, build_signer
from shared.refund_policy import RefundPolicyConfig
from shared.tracing import Tracing, build_tracing

log = logging.getLogger("worker")


def create_app(
    *,
    model: BaseChatModel | None = None,
    specialists: Specialists | None = None,
    tracing: Tracing | None = None,
    policy: RefundPolicyConfig | None = None,
    store: DisputeStore | None = None,
    queue: DisputeQueue | None = None,
    refunds: DisputeQueue | None = None,
    judge_jobs: JudgeJobs | None = None,
    signer: TokenSigner | None = None,
    delegation: DelegationSettings | None = None,
    recursion_limit: int = RECURSION_LIMIT,
    retry_delay_seconds: float = RETRY_DELAY_SECONDS,
    shutdown_grace_seconds: float = 30.0,
) -> FastAPI:
    """Build the worker. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if model is None:
            from shared.llm import build_chat_model

        http = None
        if specialists is None:
            http = httpx.AsyncClient(timeout=60)
        app.state.store, pool = (store, None) if store is not None else await open_store()
        app.state.queue = queue or ServiceBusQueue(os.environ["SERVICEBUS_NAMESPACE"], os.environ["SERVICEBUS_QUEUE"])
        # Approved refunds go to their own queue; this worker may only SEND to it (ADR-0021).
        app.state.refunds = refunds or ServiceBusQueue(os.environ["SERVICEBUS_NAMESPACE"], os.environ["REFUNDS_QUEUE"])
        signing, settings = (signer, delegation) if signer is not None else build_signer()
        worker_tracing = tracing or build_tracing()
        runner = TriageRunner(
            store=app.state.store,
            queue=app.state.queue,
            refunds=app.state.refunds,
            graph=build_graph(model or build_chat_model()),
            specialists=specialists or HttpSpecialists(
                http,
                fraud_url=os.environ["FRAUD_AGENT_URL"],
                ledger_url=os.environ["LEDGER_AGENT_URL"],
                core_url=os.environ["CORE_SYSTEMS_URL"],
            ),
            policy=policy or RefundPolicyConfig.from_env(),
            tracing=worker_tracing,
            signer=signing,
            delegation=settings,
            recursion_limit=recursion_limit,
            retry_delay_seconds=retry_delay_seconds,
            shutdown_grace_seconds=shutdown_grace_seconds,
            judge_jobs=judge_jobs if judge_jobs is not None else build_judge_jobs(),
        )
        app.state.consumer = asyncio.create_task(runner.run_forever())
        log.info("worker started: consuming the dispute queue")
        yield
        # Shutdown (a deploy, a node drain): stop taking messages, let runs in progress finish.
        # Anything that does not finish in time was never completed, so the queue redelivers it.
        app.state.consumer.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.consumer
        worker_tracing.shutdown()
        for bus in (app.state.queue, app.state.refunds):
            if isinstance(bus, ServiceBusQueue):
                await bus.close()
        if pool is not None:
            await pool.close()
        if http is not None:
            await http.aclose()

    app = FastAPI(title="Dispute Triage Worker", version="1.0.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        """Liveness: the process is up AND still consuming. A dead consumer loop means a restart."""
        consuming = not request.app.state.consumer.done()
        return JSONResponse({"status": "ok" if consuming else "consumer stopped"}, status_code=200 if consuming else 503)

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        ready = await request.app.state.store.ping()
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
