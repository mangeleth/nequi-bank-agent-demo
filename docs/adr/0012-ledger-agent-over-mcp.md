# ADR-0012: Ledger Agent reaches Core Banking through MCP, and its figures are verified in code

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M4

## Context
The Ledger Agent states what the ledger shows happened to a disputed transaction
(`LedgerReconciliation`). Two forces shape it:

1. Tools written per agent (as in the Fraud Agent, ADR-0011) do not scale across many agents and
   teams. MCP (Model Context Protocol) is a standard for offering tools to AI agents: the owner
   of a system publishes tools once, and any agent can discover and call them.
2. The refund policy pays `debited - credited` (ADR-0007). If those figures came from an LLM,
   a mis-copy or a successful prompt injection would change the amount paid.

## Decision
- **MCP server in Core Systems** at `/mcp`, built with the official MCP SDK. It is a second front
  door onto the same `LedgerRepository` port as the REST API (ADR-0008), so both always agree.
  Tools: `get_transaction`, `get_refund_history` (read-only).
- **Stateless Streamable HTTP with JSON responses**, so any replica can serve any request and no
  session affinity is needed. DNS-rebinding protection is on, with an allow-list of host names.
- **Identity on the connection, not in tool arguments:** the Ledger Agent opens one MCP
  connection per request with `X-Customer-Id` set from the verified JWT (ADR-0009). The server
  reads the header; the model cannot see or set it. (The Fraud Agent achieves the same with
  LangChain's `ToolRuntime`; MCP achieves it at the transport.)
- **Client-side allow-list:** the agent offers the model only tools named in `ALLOWED_TOOLS`,
  refuses a tool that asks for an identity argument, and refuses to run if an expected tool is
  missing. A changed or compromised server cannot hand the model new capabilities.
- **Authorize before, verify after:** our code fetches the disputed transaction first (404 if it
  is not the caller's, no model call). After the agent answers, its `settlement_status`,
  `debited_amount`, `credited_amount`, `currency`, and `transaction_id` must equal that record,
  otherwise the service returns 502 and the dispute goes to a human.
- **What the model contributes:** the plain-language `summary` and the choice of lookups. The
  numeric fields could be produced by code alone; they pass through the model here only because
  they are then verified. A model is not trusted with a number that code can supply.

## Consequences
- + Core Banking's tools are published once and usable by any MCP-capable agent or framework.
- + The amount a refund would pay is guaranteed to equal the system of record.
- + Adding a tool on the server does not silently widen what the model can do.
- - One more protocol and dependency (`mcp` 2.x) to operate and keep current.
- - The `X-Customer-Id` header is trusted because the endpoint is internal (ClusterIP).
- - Tool descriptions come from the server and become part of the prompt, so a compromised
  server could try to steer the model through them (tool poisoning). The allow-list limits
  which tools exist, not what their descriptions say.

## Production delta
MCP authorization per the specification (OAuth 2.1 resource server validating the user's token)
or mTLS between services, instead of a trusted header; pin and review tool descriptions and
schemas (hash check) before offering them to a model; an MCP gateway for central policy, rate
limits, and audit; network policy so only agent pods reach `/mcp`; contract tests between the
MCP server and its clients in CI.
