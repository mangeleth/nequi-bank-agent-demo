# ADR-0006: Pydantic v2 contracts as the trust boundary for LLM output

- **Status:** Accepted (human-approval rule amended by ADR-0007)
- **Date:** 2026-10-01
- **Milestone:** M2

## Context
Agents exchange data produced by LLMs, which can be malformed, contradictory, or manipulated
by prompt injection in customer-written text. In a payments system, a wrong number or an
invented field (`refund_approved: true`) must never reach a money-moving step.

## Decision
All messages between UI, supervisor, and agents use the models in `shared/schemas.py`:
- **Money is `Decimal`**, max 2 decimals and 15 digits; floats (including JSON numbers like
  `150000.5`) are rejected. Money travels in JSON as a string (`"150000.00"`). Floats are used
  only for scores/probabilities.
- **`extra="forbid"`** on every model: unknown fields are errors, so the LLM cannot add fields.
- **`frozen=True`**: contracts are immutable once validated.
- **No `user_id` in any request contract.** The caller's identity comes only from the verified
  JWT (Step 3), which prevents IDOR via parameter tampering.
- **Constrained identifiers** (`TX-` + digits) so free text cannot ride in an ID field.
- **Cross-field invariants** enforced in validators: fraud `risk_level` must match `risk_score`;
  `REFUND_RECOMMENDED` requires `refund_amount`, `requires_human_approval=True`, and status
  `PENDING_HUMAN_APPROVAL`. There is no "refund executed" decision an agent can produce.

## Consequences
- + Invalid LLM output fails loudly at the boundary, with a precise error the supervisor can
  feed back to the model for a retry.
- + Human-in-the-loop for refunds is a type-level guarantee, not a prompt instruction.
- - Strict contracts mean more validation failures to handle (retry or escalate).
- - Every service image must ship the `shared` package (built into each Dockerfile).

## Production delta
Version contracts (`schema_version` field) and publish them in a schema registry; generate
JSON Schema / OpenAPI for consumers; use integer minor units at the ledger boundary to match
core banking and ISO 20022 messages; add currency per ISO 4217 when multi-currency.
