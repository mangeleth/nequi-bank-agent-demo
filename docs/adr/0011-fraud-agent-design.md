# ADR-0011: Fraud Agent — the model chooses what to look up, code decides for whom

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M3

## Context
The Fraud Agent turns raw risk signals into a `FraudAssessment` that feeds the refund policy
(ADR-0007). It reads customer-written text, so it must be assumed that prompt injection will
sometimes succeed. The design must stay safe even when the model is fooled.

## Decision
- **Framework:** LangChain `create_agent` (an LLM in a loop with tools, running on LangGraph).
- **Order of work per request:** authenticate (verify JWT) -> authorize in code (the disputed
  transaction must belong to the caller, checked against Core Systems) -> only then call the
  model. A failed check returns 401/404 and costs no model call.
- **Identity outside the model:** tools receive the verified `CallerIdentity` through
  `ToolRuntime.context`. LangChain hides that parameter from the tool description, so the model
  cannot see, set, or override it. Tool results have `customer_id` removed.
- **Minimal, validated tools:** two read-only lookups whose only argument is a `transaction_id`
  matching `TX-<digits>`. The pattern also blocks path traversal (`TX-1/../../x`) into other
  Core Systems endpoints. The agent has no tool that changes anything.
- **Validated output with self-correction:** the final answer must be a `FraudAssessment`
  (`ToolStrategy`). A validation failure is returned to the model as an error so it can correct
  itself; the assessment must also be for the disputed transaction.
- **Bounded loop:** at most 6 model calls per request (`ModelCallLimitMiddleware`).
- **Untrusted text is labelled:** the customer's description is wrapped in
  `<customer_description>` and the system prompt explains it is evidence, not instructions.
  This reduces successful injections; the structural controls above are what contain them.
- **Fail closed:** if no valid assessment is produced, the service returns 502 and the dispute
  goes to human review. It never returns a guessed or partial assessment.

## Consequences
- + A fooled model can at most write a poor assessment of the caller's own transaction; it cannot
  read other customers' data (verified by tests that script a compromised model).
- + Unauthorized and unauthenticated requests cost nothing in model usage.
- - An assessment takes 3-5 seconds (two model calls plus lookups).
- - The model could still be talked into a lower risk score for the caller's own transaction.
  Mitigated by the refund policy's limits, which do not depend on the model (ADR-0007).

## Production delta
Restrict lookups to the disputed transaction and explicitly related ones; forward the user's
token to Core Systems instead of a trusted header (ADR-0008); per-customer rate limits;
timeouts and circuit breakers on tool calls; an evaluation set run in CI for every prompt,
tool, or model change; injection-detection as an additional signal; full tracing (M5).
