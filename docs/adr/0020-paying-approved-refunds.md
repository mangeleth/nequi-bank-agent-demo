# ADR-0020: The worker pays approved refunds from the saved decision; definite refusals go to a person

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 11)

## Context
Until now an approved dispute ended as `refund_approved` and no money moved. The ledger can now
pay exactly once per idempotency key, with its own rules (ADR-0019). The worker has to call it.

Two things can go wrong around a payment:
- the worker dies, or the ledger does not answer, after the decision but before the outcome is
  recorded, so the queue redelivers
- the ledger gives a definite no, for example `amount_mismatch`: it owes a different amount than
  our policy approved

## Decision
- **The decision is saved before any money moves.** The worker finishes the run (`running ->
  finished`, `refund_approved`), and only then pays.
- **Payment works from the saved approval, never from a new run of the graph.** A redelivered
  message that finds a finished, still-approved dispute only pays it. The model and the agents
  are not asked again, so a second run cannot reach a different decision about money that may
  already have moved.
- **The idempotency key is `dispute:<dispute id>`**, so the ledger pays at most once however
  often this step runs.
- **The amount is the policy's `approved_amount`**, which came from the ledger's figures, never
  from the model.
- **Three outcomes, three paths:**

  | The ledger answers | Meaning | The worker |
  |---|---|---|
  | `201` paid, or `200` replay | Paid (now or before) | `refund_approved -> refund_paid`, with the ledger's refund ID |
  | `404`, `409`, `422` (e.g. `amount_mismatch`) | A definite no | `-> pending_human_approval`. Not retried: the same request gets the same answer |
  | No answer: network error, timeout, `5xx` | Unknown whether it paid | Retry through the queue with the same key. After the last delivery, a person checks the ledger for that key |

- **A refusal goes to a person, not to an agent.** A refusal means our evidence and the bank's
  records disagree about money. Deciding which is right is the kind of decision this system keeps
  away from a model.
- **"Paid" is said only after the ledger confirms.** The customer message for `refund_paid` uses
  the ledger's amount; a refused or unknown payment says the refund "could not be paid
  automatically" and never "paid".
- `store.settle()` changes the business status only from `refund_approved`, so two workers
  cannot both record a payment, and every outcome is a line in the audit trail.

## Consequences
- + A worker that dies at any point before, during, or after paying cannot pay twice, and cannot
  leave an approved dispute unpaid without a person being told.
- + Payment retries cost no model calls.
- - A dispute can show `refund_approved` with `finished` for a few seconds: the decision is saved,
  the payment is not yet confirmed. That state is true, and the customer message says "approved,
  not yet paid".
- - Paying happens at the pace of triage. A burst of approvals becomes a burst of ledger writes.

## Production delta
Approved refunds go to their own queue, drained at a rate the core banking system can absorb,
with a dead-letter queue and an alert (the roadmap's "rate-limited drain"). A reconciliation job
compares `refund_paid` disputes with the ledger daily. A person's decision on a refused refund
is recorded with who decided and why, and a corrected amount is paid with a new key.
