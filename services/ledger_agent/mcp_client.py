"""MCP client for the Ledger Agent: connects to Core Banking's MCP server (ADR-0012).

One connection is opened per request, as the verified caller: the customer ID is an HTTP header
on the connection, set here from the JWT. It is not a tool argument, so the model cannot set it.

An MCP server tells the client which tools it has. We do not hand that list to the model as-is:
only tools on our allow-list are used, and a tool that asks for an identity argument is refused.
That limits the damage if the server is changed or compromised.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx2
from langchain_core.tools import StructuredTool
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from shared.auth import CallerIdentity

ALLOWED_TOOLS = frozenset({"get_transaction", "get_refund_history"})
IDENTITY_WORDS = ("customer", "user", "identity", "owner")


class McpToolRejected(Exception):
    """The MCP server's tool list is not what we reviewed and approved."""


class CoreBanking:
    """Core Banking as seen through one MCP connection, bound to one verified customer."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def _call(self, name: str, arguments: dict) -> tuple[bool, str]:
        result = await self._client.call_tool(name, arguments)
        text = "\n".join(block.text for block in result.content if getattr(block, "text", None))
        return not result.is_error, text

    async def get_transaction(self, transaction_id: str) -> dict | None:
        """For our own code (authorization and verification), not for the model."""
        ok, text = await self._call("get_transaction", {"transaction_id": transaction_id})
        if not ok:
            raise RuntimeError(f"core banking lookup failed: {text}")
        record = json.loads(text)
        return None if "error" in record else record

    async def tools_for_model(self) -> list[StructuredTool]:
        """The approved MCP tools, wrapped so a LangChain agent can call them."""
        tools = []
        for spec in (await self._client.list_tools()).tools:
            if spec.name not in ALLOWED_TOOLS:
                continue  # a tool we never reviewed is never shown to the model
            parameters = spec.input_schema.get("properties", {})
            if any(word in name.lower() for name in parameters for word in IDENTITY_WORDS):
                raise McpToolRejected(f"tool {spec.name!r} asks for an identity argument: {sorted(parameters)}")
            tools.append(self._as_langchain_tool(spec.name, spec.description or spec.name, spec.input_schema))

        missing = ALLOWED_TOOLS - {tool.name for tool in tools}
        if missing:
            raise McpToolRejected(f"core banking no longer offers: {sorted(missing)}")
        return tools

    def _as_langchain_tool(self, name: str, description: str, input_schema: dict) -> StructuredTool:
        async def run(**arguments) -> str:
            ok, text = await self._call(name, arguments)
            return text if ok else f"Tool error: {text}"  # the model sees the error and can correct

        return StructuredTool.from_function(coroutine=run, name=name, description=description, args_schema=input_schema)


@asynccontextmanager
async def open_core_banking(
    url: str, caller: CallerIdentity, http_factory: Callable[..., httpx2.AsyncClient] = httpx2.AsyncClient
) -> AsyncIterator[CoreBanking]:
    """Connect to the Core Banking MCP server as the verified caller."""
    async with http_factory(headers={"X-Customer-Id": caller.user_id}, timeout=10) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            yield CoreBanking(client)
