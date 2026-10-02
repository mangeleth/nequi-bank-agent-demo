"""Refund payer: takes approved refunds from the refunds queue and pays them, at a controlled
pace (ADR-0021).

A separate deployment with its own identity. It may receive from the refunds queue, read the
dispute records, and call Core Banking. It has no model access and cannot sign tokens, and no
customer request can reach it: like the triage worker it exposes only health endpoints.

Operations:
    REFUND_PAYMENTS_PER_SECOND  how many payments may START per second (default 2)
    REFUND_PAYMENTS_PAUSED      "true" stops paying; approved refunds wait in the queue

Run locally:  uvicorn services.supervisor.payer:create_app --factory --port 8006
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.supervisor.clients import HttpSpecialists, Specialists
from services.supervisor.payments import REFUND_MAX_DELIVERIES, REFUND_RETRY_DELAY_SECONDS, RefundPayer
from services.supervisor.queue import DisputeQueue, ServiceBusQueue
from services.supervisor.store import DisputeStore, open_store

log = logging.getLogger("payer")


def create_app(
    *,
    store: DisputeStore | None = None,
    queue: DisputeQueue | None = None,
    specialists: Specialists | None = None,
    per_second: float | None = None,
    paused: bool | None = None,
    retry_delay_seconds: float = REFUND_RETRY_DELAY_SECONDS,
) -> FastAPI:
    """Build the payer. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = httpx.AsyncClient(timeout=30) if specialists is None else None
        app.state.store, pool = (store, None) if store is not None else await open_store()
        app.state.queue = queue or ServiceBusQueue(os.environ["SERVICEBUS_NAMESPACE"], os.environ["REFUNDS_QUEUE"],
                                                   max_deliveries=REFUND_MAX_DELIVERIES)
        settings = RefundPayer.settings_from_env()
        payer = RefundPayer(
            store=app.state.store, queue=app.state.queue,
            specialists=specialists or HttpSpecialists(http, core_url=os.environ["CORE_SYSTEMS_URL"]),
            per_second=per_second if per_second is not None else settings["per_second"],
            paused=paused if paused is not None else settings["paused"],
            retry_delay_seconds=retry_delay_seconds,
        )
        app.state.paused = payer.paused
        app.state.consumer = asyncio.create_task(payer.run_forever())
        log.info("refund payer started: %s per second%s", payer.per_second, " (PAUSED)" if payer.paused else "")
        yield
        app.state.consumer.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.consumer
        if isinstance(app.state.queue, ServiceBusQueue):
            await app.state.queue.close()
        if pool is not None:
            await pool.close()
        if http is not None:
            await http.aclose()

    app = FastAPI(title="Refund Payer", version="1.0.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        """Liveness: the process is up AND still consuming (or deliberately paused)."""
        consuming = not request.app.state.consumer.done()
        status = "paused" if request.app.state.paused else "ok"
        return JSONResponse({"status": status if consuming else "consumer stopped"},
                            status_code=200 if consuming else 503)

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        ready = await request.app.state.store.ping()
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
