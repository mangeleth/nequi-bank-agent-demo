"""Tools the Fraud Agent's LLM may ask us to run (ADR-0011).

The LLM chooses WHICH transaction to look up. Our code decides FOR WHOM: the customer identity
comes from `AgentContext`, which is filled in from the verified JWT and is never shown to the
model. LangChain hides the `runtime` parameter from the tool description the model receives.
"""

from dataclasses import dataclass

import httpx
from langchain.tools import ToolRuntime, tool

from shared.auth import CallerIdentity
from shared.schemas import TransactionId

NOT_FOUND = "No such transaction for this customer."


@dataclass(frozen=True)
class AgentContext:
    """Per-request facts set by our code, outside the model."""

    caller: CallerIdentity  # from shared.auth.verify_token
    core: httpx.AsyncClient  # client for the Core Systems API


async def core_get(context: AgentContext, path: str) -> dict | None:
    """GET a Core Systems resource as the verified caller. None if it is not theirs / not found."""
    response = await context.core.get(path, headers={"X-Customer-Id": context.caller.user_id})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    body.pop("customer_id", None)  # the model never needs, and never sees, a customer ID
    return body


@tool
async def get_transaction(transaction_id: TransactionId, runtime: ToolRuntime[AgentContext]) -> dict | str:
    """Get one of the customer's transactions: amount, masked recipient account, creation time,
    and settlement status (settled, pending, failed, or reversed) with debited and credited amounts."""
    return await core_get(runtime.context, f"/v1/core-banking/transactions/{transaction_id}") or NOT_FOUND


@tool
async def get_risk_signals(transaction_id: TransactionId, runtime: ToolRuntime[AgentContext]) -> dict | str:
    """Get the risk engine's signals for one of the customer's transactions: engine_score (0-1),
    whether the recipient is known and how old their account is, the amount compared with the
    customer's average (1.0 = usual), new device in the last 24h, and transfers in the last 24h."""
    return await core_get(runtime.context, f"/v1/risk/transactions/{transaction_id}/signals") or NOT_FOUND


TOOLS = [get_transaction, get_risk_signals]
