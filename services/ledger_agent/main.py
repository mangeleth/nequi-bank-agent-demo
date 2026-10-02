"""Ledger Agent service: POST /v1/ledger/reconciliations.

Order of work for each request (ADR-0012):
  1. Authenticate: verify the JWT.
  2. Connect to Core Banking over MCP as that customer.
  3. Authorize: our code fetches the disputed transaction; 404 if it is not the caller's.
  4. Run the agent with the approved MCP tools.
  5. Verify: the agent's figures must equal the record fetched in step 3.

Run locally:  uvicorn services.ledger_agent.main:create_app --factory --port 8003
"""

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import httpx2
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel

from services.ledger_agent.agent import ReconciliationFailed, reconcile, verify_against_ledger
from services.ledger_agent.mcp_client import McpToolRejected, open_core_banking
from shared.auth import AuthError, AuthSettings, CallerIdentity, bearer_token, verify_token
from shared.schemas import DisputeRequest, LedgerReconciliation
from shared.tracing import Tracing, build_tracing

log = logging.getLogger("ledger_agent")


def create_app(
    *,
    auth: AuthSettings | None = None,
    model: BaseChatModel | None = None,
    core_url: str | None = None,
    http_factory: Callable[..., httpx2.AsyncClient] = httpx2.AsyncClient,
    tracing: Tracing | None = None,
) -> FastAPI:
    """Build the app. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if model is None:
            from shared.llm import build_chat_model  # imported here so tests need no Azure setup

        app.state.auth = auth or AuthSettings.from_env()
        app.state.model = model or build_chat_model()
        app.state.core_url = (core_url or os.environ["CORE_SYSTEMS_URL"]).rstrip("/")
        app.state.tracing = tracing or build_tracing()
        yield
        app.state.tracing.shutdown()

    app = FastAPI(title="Ledger Agent", version="1.0.0", lifespan=lifespan)

    def caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> CallerIdentity:
        try:
            return verify_token(bearer_token(authorization), request.app.state.auth)
        except AuthError as exc:
            log.warning("authentication failed: %s", exc)
            raise HTTPException(401, "invalid or missing token", {"WWW-Authenticate": "Bearer"}) from exc

    @app.post("/v1/ledger/reconciliations", response_model=LedgerReconciliation)
    async def create_reconciliation(
        dispute: DisputeRequest,
        request: Request,
        identity: Annotated[CallerIdentity, Depends(caller)],
        traceparent: Annotated[str | None, Header()] = None,
    ) -> LedgerReconciliation:
        state = request.app.state
        reconciliation, failure = None, None
        # Called by the supervisor, the `traceparent` header makes this run part of its trace.
        callbacks, _ = state.tracing.start(traceparent)

        # Outcomes are recorded here and turned into HTTP errors after the connection closes:
        # an exception raised inside the MCP connection block surfaces wrapped in an
        # ExceptionGroup, which FastAPI would report as a 500.
        async with open_core_banking(f"{state.core_url}/mcp", identity, http_factory) as core_banking:
            # Authorization in code, before any model call.
            record = await core_banking.get_transaction(dispute.transaction_id)
            if record is not None:
                try:
                    tools = await core_banking.tools_for_model()
                    reconciliation = await reconcile(state.model, tools, dispute, callbacks)
                    verify_against_ledger(reconciliation, dispute, record)
                except (ReconciliationFailed, McpToolRejected) as exc:
                    failure = exc

        if record is None:
            raise HTTPException(404, "transaction not found")
        if failure is not None:
            log.error("reconciliation failed for %s: %s", dispute.transaction_id, failure)
            raise HTTPException(502, "reconciliation unavailable; escalate to human review")
        return reconciliation

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with http_factory(timeout=3) as http:
                ready = (await http.get(f"{request.app.state.core_url}/readyz")).status_code == 200
        except httpx2.HTTPError:
            ready = False
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
