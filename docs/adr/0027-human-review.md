# ADR-0027: A person reviews the disputes the system sends to them; approving pays the ledger's amount

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M8

## Context
Many disputes end as `pending_human_approval`: over the automatic limit, high fraud risk, a
customer over their monthly limits, a run that failed. Until now nothing let a person act on them.
A human decision moves money, so it needs the same guarantees as an automatic one.

## Decision
- **Reviewer identity.** A token from the same identity provider with the `dispute-reviewer` role
  and an `ops-` identity (`verify_reviewer_token`). A customer token never passes, even with a
  smuggled role claim; tests cover it.
- **Review API** in the intake service, every route reviewer-only: the queue (oldest first), one
  dispute with its evidence, policy checks, the LLM judge's verdict (ADR-0026) and audit trail,
  and a decision.
- **Approve pays what the ledger shows owed, read by code at the moment of approval.** The
  reviewer cannot type an amount (the request rejects unknown fields). If the ledger shows nothing
  owed, the approval is refused. The approval is recorded as route `human_approved` with
  `approved_by`, goes on the refunds queue, and the **same refund payer** pays it with the same
  idempotency key, so it is paid at most once (ADR-0020, ADR-0021).
- **Reject** closes the dispute as `rejected`; the customer is told a person reviewed it.
- **A reason is required** for every decision (5 to 500 characters), and the decision is one line
  of the audit trail: who, what, how much, why.
- **Decided once.** The store changes the dispute only while it is still waiting for a person, so
  two reviewers cannot both decide it.
- **If the approved refund cannot be queued**, the dispute goes back to the review queue with
  that reason; it is never left approved and unpaid.
- The demo UI has a reviewer tab (`👤 Revisión (supervisor)`) with synthetic reviewers.

## Verified on the cluster
A 450.000 COP dispute (over the automatic limit) waited for a person. In the public page, as
`ops-ana`, approved with a reason: `refund_approved` (route `human_approved`, by `ops-ana`), paid
by the payer 1 second later, one ledger refund of 450.000, every step in the audit trail.

## Consequences
- + The human-in-the-loop path is real and as safe as the automatic one: same amount source, same
  payer, same once-only payment.
- - In the public demo anyone can act as a synthetic reviewer (ADR-0024).

## Production delta
Reviewers sign in with the bank's identity provider and MFA; four-eyes approval above a threshold
(a second reviewer); separation of duties (a reviewer cannot review their own disputes); SLAs and
assignment for the queue; reasons chosen from a list plus free text; decisions sampled for QA.
