"""Fraud Agent service: POST /v1/fraud/assessments.

Order of work for each request (ADR-0011):
  1. Authenticate: verify the JWT. No token, no work.
  2. Authorize: confirm in code that the disputed transaction belongs to the caller.
     The model is not involved in this decision and is not called if it fails.
  3. Only then run the agent, passing the verified identity outside the model.

Run locally:  uvicorn services.fraud_agent.main:create_app --factory --port 8002
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel

from services.fraud_agent.agent import AssessmentFailed, assess, build_fraud_agent
from services.fraud_agent.tools import AgentContext, core_get
from shared.auth import AuthError, AuthSettings, CallerIdentity, bearer_token, verify_token
from shared.schemas import DisputeRequest, FraudAssessment

log = logging.getLogger("fraud_agent")


def create_app(
    *,
    auth: AuthSettings | None = None,
    core: httpx.AsyncClient | None = None,
    model: BaseChatModel | None = None,
) -> FastAPI:
    """Build the app. Arguments default to real, env-configured dependencies; tests pass fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if model is None:
            from shared.llm import build_chat_model  # imported here so tests need no Azure setup

        app.state.auth = auth or AuthSettings.from_env()
        app.state.core = core or httpx.AsyncClient(base_url=os.environ["CORE_SYSTEMS_URL"], timeout=10)
        app.state.agent = build_fraud_agent(model or build_chat_model())
        yield
        if core is None:
            await app.state.core.aclose()

    app = FastAPI(title="Fraud Agent", version="1.0.0", lifespan=lifespan)

    def caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> CallerIdentity:
        try:
            return verify_token(bearer_token(authorization), request.app.state.auth)
        except AuthError as exc:
            log.warning("authentication failed: %s", exc)  # detail goes to logs, not to the client
            raise HTTPException(401, "invalid or missing token", {"WWW-Authenticate": "Bearer"}) from exc

    @app.post("/v1/fraud/assessments", response_model=FraudAssessment)
    async def create_assessment(
        dispute: DisputeRequest, request: Request, identity: Annotated[CallerIdentity, Depends(caller)]
    ) -> FraudAssessment:
        context = AgentContext(caller=identity, core=request.app.state.core)

        # Authorization in code, before any model call.
        if await core_get(context, f"/v1/core-banking/transactions/{dispute.transaction_id}") is None:
            raise HTTPException(404, "transaction not found")

        try:
            return await assess(request.app.state.agent, dispute, context)
        except AssessmentFailed as exc:
            log.error("assessment failed for %s: %s", dispute.transaction_id, exc)
            raise HTTPException(502, "assessment unavailable; escalate to human review") from exc

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        try:
            ready = (await request.app.state.core.get("/readyz")).status_code == 200
        except httpx.HTTPError:
            ready = False
        return JSONResponse({"status": "ready" if ready else "not ready"}, status_code=200 if ready else 503)

    return app
