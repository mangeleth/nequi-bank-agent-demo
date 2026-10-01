# ADR-0007: Tiered refund approval — deterministic auto-approval for small, clear cases

- **Status:** Accepted (amends the human-approval rule in ADR-0006)
- **Date:** 2026-10-01
- **Milestone:** M2

## Context
ADR-0006 required a human to approve every refund. At Nequi's scale (28M users), routing every
5.000 COP failed transfer to a person costs more than the refund and makes customers wait days.
But the LLM is the component that can be manipulated (prompt injection in the dispute
description), so it must never be the one that approves money movement.

## Decision
Split **recommending** from **approving**:

1. The LLM supervisor produces a `DisputeVerdict`: a recommendation only. It has no field or
   decision value that approves or executes anything.
2. `shared/refund_policy.evaluate()` — plain Python, no LLM — decides `AUTO_APPROVED` or
   `HUMAN_REQUIRED` and records every check in a `RefundApproval` for audit.
3. **All** checks must pass for auto-approval; any failure routes to a human:

   | Check | Source of truth |
   |---|---|
   | Kill switch `AUTO_REFUND_ENABLED` is on | Config |
   | Verdict, ledger, and fraud refer to the same transaction | Contracts |
   | Ledger status is `FAILED` (not pending, settled, or already reversed) | Core banking |
   | Recommended amount equals the ledger discrepancy | Core banking |
   | Fraud risk level is `LOW` | Risk engine |
   | Amount <= per-dispute limit | Config |
   | Customer's auto-refund count in window + 1 <= limit | Core banking history |
   | Customer's auto-refund total in window + amount <= limit | Core banking history |

4. The refunded amount is always the **ledger's** discrepancy, never the LLM's or the customer's.
5. Thresholds use **facts from systems of record**, never the LLM's self-reported confidence.

**Starting values** (live values belong to config — `RefundPolicyConfig` / ConfigMap — so changing
them is an operational change, not a new ADR):

| Setting | Env var | Start |
|---|---|---|
| Max per dispute | `AUTO_REFUND_MAX_AMOUNT` | 100.000 COP |
| Max auto-refunds per customer per window | `AUTO_REFUND_MAX_COUNT` | 3 |
| Max auto-refund total per customer per window | `AUTO_REFUND_MAX_TOTAL` | 200.000 COP |
| Window | `AUTO_REFUND_WINDOW_DAYS` | 30 days |
| Kill switch | `AUTO_REFUND_ENABLED` | `true` |

## Consequences
- + Most small failed transfers resolve in seconds; humans focus on large or risky cases.
- + A successful prompt injection can at worst produce a wrong recommendation, which the policy
  rejects (amount mismatch, ledger status) or a human reviews.
- + Per-customer limits stop "many disputes just under the limit" abuse.
- - The policy depends on accurate refund history from core banking; if unavailable, the service
  must fail closed (route to human), never assume zero history.
- - Limits need tuning against real dispute and fraud data.

## Production delta
Policy versioning with change approval (four-eyes) and audit of who changed limits; limits per
customer segment and risk tier; an idempotency key on refund execution so retries cannot pay
twice; monitoring of auto-approval rate and post-refund fraud loss; regular review by risk and
compliance (SFC); shadow mode (log decisions without executing) before raising limits.
