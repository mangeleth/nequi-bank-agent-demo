"""Request-scoped dependencies shared by the routers."""

from typing import Annotated

from fastapi import Depends, Header, Request

from services.core_systems.ports import LedgerRepository, RiskRepository

# Set by the calling agent from the *verified* JWT (Step 3), never from LLM output.
# Inside the cluster this API is ClusterIP-only; production adds mTLS / service identity.
CallerCustomerId = Annotated[str, Header(alias="X-Customer-Id", pattern=r"^user-[0-9]{4,12}$")]


def _ledger(request: Request) -> LedgerRepository:
    return request.app.state.ledger


def _risk(request: Request) -> RiskRepository:
    return request.app.state.risk


Ledger = Annotated[LedgerRepository, Depends(_ledger)]
Risk = Annotated[RiskRepository, Depends(_risk)]
