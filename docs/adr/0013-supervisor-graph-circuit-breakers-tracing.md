# ADR-0013: Supervisor as a cyclic LangGraph with code-enforced circuit breakers, traced to Langfuse

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M5

## Context
The supervisor coordinates the Fraud and Ledger agents and produces a `DisputeVerdict`. A
supervisor is a cycle (agents report back and it decides again), and a cycle driven by an LLM
can loop, skip required evidence, or stop early. In a first end-to-end run the model finished
after the ledger lookup on a failed 50.000 COP transfer, reasoning that "no refund is required",
although it asked for a fraud assessment in two near-identical cases. Temperature 0 did not
prevent this; it would only have repeated it.

## Decision
- **Cyclic `StateGraph`** (`services/supervisor/graph.py`): `supervisor` -> `ledger_agent` /
  `fraud_agent` -> back to `supervisor` -> `write_verdict` -> `policy`, with an `escalate` node
  that hands the dispute to human operations.
- **The model decides only through structured output:** `Route` (one of `ledger_agent`,
  `fraud_agent`, `finish`) and `VerdictDraft`. Output that does not match the schema is treated
  as "no decision" and escalated.
- **Circuit breakers in plain code, in layers:**
  1. *Turn counters* in the graph state (`turns`, `ledger_calls`, `fraud_calls`), incremented by
     the nodes, never by the model.
  2. *Loop-breaking conditional edge* (`breaker()`): at most 6 supervisor turns and 2 calls per
     agent (one retry). A tripped limit routes to `escalate` whatever the model asked for.
  3. *Hard `recursion_limit=15`* on every run, as a backstop if the counters have a bug. The
     longest legitimate path is 11 steps. `GraphRecursionError` becomes an escalation, not a 500.
- **Required evidence is a rule in code:** a verdict needs ledger evidence; when the ledger shows
  a failed transfer with money missing, a fraud assessment is required and the edge sends the
  dispute to the Fraud Agent even if the model chose `finish`. The model keeps discretion where
  it is safe: the order of lookups, retries, and skipping the Fraud Agent when no refund is
  possible (settled, pending, reversed).
- **The verdict is a recommendation.** `policy` runs `shared/refund_policy.evaluate()` with the
  ledger's figures and the customer's refund history fetched by code (ADR-0007). If the history
  is unavailable the dispute is escalated: fail closed.
- **Authenticate, then authorize in code, then run the graph** (same order as the agents).
- **The customer's token is forwarded** to each agent, which verifies it again. It travels in
  the graph's runtime context, not in the state, so it reaches neither the model nor the traces.
- **Tracing:** every run gets a Langfuse trace through `CallbackHandler`, with the customer and
  login session attached, and the trace URL is returned in the `TriageResult`. Tracing is an
  observer: if Langfuse is unreachable or not configured, triage still works. Keys come from
  Key Vault (ADR-0005). No masking is applied (ADR-0003, synthetic data).

## Consequences
- + A looping, confused, or manipulated supervisor ends in human review, never in an unbounded
  loop or an unapproved payment.
- + Every decision is explainable twice: the `steps` list in the result, and the full trace
  (model inputs and outputs, latency, tokens, cost) in Langfuse.
- + The supervisor skips the Fraud Agent when no refund is possible, saving a model run.
- - A triage takes 5-15 seconds (3-4 supervisor model calls plus the agents' own).
- - Supervisor and agents write separate traces; they are not yet joined into one.
- - Routing rules now live in two places (prompt and code); the code is authoritative.

## Production delta
One distributed trace across supervisor and agents (propagate trace context); run triage
asynchronously (queue plus status endpoint) instead of holding an HTTP request open; a durable
checkpointer so a human approval can pause and resume the graph (`interrupt`); persist verdicts
under the dispute ID for idempotency; an evaluation set in CI scored on routing and verdicts;
alerts on escalation rate and breaker trips; PII masking before traces leave the network.
