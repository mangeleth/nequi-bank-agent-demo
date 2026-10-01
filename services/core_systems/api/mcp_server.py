"""Core Banking over MCP (Model Context Protocol), served at /mcp (ADR-0012).

MCP is a standard way to offer tools to AI agents: a client can ask "which tools do you have?"
and then call them, without custom integration code per agent. This module is a second front
door onto the same `LedgerRepository` port that the REST API uses, so both always agree.

Identity works as in the REST API: the customer comes from the `X-Customer-Id` HTTP header set
by the calling service from a verified JWT. It is never a tool argument, so a model cannot set it.
The `ctx` parameter is filled in by the MCP SDK and is not part of the tool description.
"""

import os
import re
from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette

from services.core_systems.ports import LedgerRepository
from shared.schemas import TransactionId

NOT_FOUND = "No such transaction for this customer."
_CUSTOMER_ID = re.compile(r"^user-[0-9]{4,12}$")

# Only requests addressed to these host names are served (protection against DNS rebinding).
DEFAULT_ALLOWED_HOSTS = (
    "core-systems,core-systems.disputes,core-systems.disputes.svc.cluster.local,127.0.0.1:*,localhost:*"
)


def _caller(ctx: Context) -> str:
    customer_id = (ctx.headers or {}).get("x-customer-id", "")
    if not _CUSTOMER_ID.fullmatch(customer_id):
        raise ValueError("missing or malformed X-Customer-Id header")
    return customer_id


def build_mcp_app(get_ledger: Callable[[], LedgerRepository]) -> Starlette:
    """Build the MCP server and return it as an ASGI app with one route: POST /mcp."""
    server = MCPServer(
        "core-banking",
        instructions="Read-only access to the calling customer's transactions and refund history.",
    )

    @server.tool()
    async def get_transaction(transaction_id: TransactionId, ctx: Context) -> dict:
        """Get one of the customer's transactions: amount, masked recipient account, creation time,
        and settlement status (settled, pending, failed, or reversed) with the debited and credited
        amounts. Amounts are decimal strings in COP."""
        tx = await get_ledger().get_transaction(_caller(ctx), transaction_id)
        if tx is None:
            return {"error": NOT_FOUND}
        return tx.model_dump(mode="json", exclude={"customer_id"})

    @server.tool()
    async def get_refund_history(ctx: Context, window_days: Annotated[int, Field(ge=1, le=365)] = 30) -> dict:
        """Get how many automatic refunds the customer received in the last `window_days` days and
        their total amount (a decimal string in COP)."""
        history = await get_ledger().get_refund_history(_caller(ctx), window_days)
        return history.model_dump(mode="json", exclude={"customer_id"})

    allowed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS).split(",")
    return server.streamable_http_app(
        # Stateless + plain JSON responses: any replica can answer any request, so the two
        # Core Systems pods need no session affinity.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[h.strip() for h in allowed_hosts]
        ),
    )
