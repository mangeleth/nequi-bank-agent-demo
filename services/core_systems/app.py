"""Core Systems service: Nequi Core Banking + Risk Engine APIs (no LLM).

These are the systems of record that the Fraud and Ledger agents query through their tools.
The data source is chosen at startup by CORE_SYSTEMS_BACKEND (ports & adapters, ADR-0008);
today only `in_memory` (synthetic data) exists.

Run locally:  uvicorn services.core_systems.app:app --reload
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.core_systems.api import core_banking, risk_engine


def _build_adapters(backend: str) -> tuple:
    if backend == "in_memory":
        from services.core_systems.adapters.in_memory import InMemoryLedger, InMemoryRisk

        return InMemoryLedger(), InMemoryRisk()
    # Fail fast at startup rather than serving with the wrong data source.
    raise ValueError(f"unknown CORE_SYSTEMS_BACKEND={backend!r} (supported: in_memory)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ledger, app.state.risk = _build_adapters(os.environ.get("CORE_SYSTEMS_BACKEND", "in_memory"))
    yield


app = FastAPI(title="Nequi Core Systems", version="1.0.0", lifespan=lifespan)
app.include_router(core_banking.router)
app.include_router(risk_engine.router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Liveness: the process is up. Never checks dependencies (that would cause restart storms)."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: backing systems reachable. Failing removes the pod from the Service, no restart."""
    ready = await request.app.state.ledger.ping() and await request.app.state.risk.ping()
    return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)
