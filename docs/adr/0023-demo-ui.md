# ADR-0023: A Streamlit demo UI that uses the system only as a customer's app would

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M7

## Context
The live demo (`docs/DEMO_SCRIPT.md`) needs a screen: submit disputes as different synthetic
customers, watch their status change, see how each was decided (agents, or a confirmed incident
with no model), and show the evaluation numbers. It must not become a back door into the system.

## Decision
- **Streamlit, one page:** the customer's app (two disputes side by side, for the covered vs.
  not-covered comparison), the reviewer's tab (ADR-0027), and the evaluation dashboard. The demo
  script is for the presenter only: `docs/DEMO_SCRIPT.md`, not shown on the public page (#18).
- **It uses only the intake API**, exactly as a customer's app would: `POST /v1/disputes` and
  `GET /v1/disputes/{id}`. It has no access to the queues, the database, the model, or Core
  Systems. Its identity, `id-demo-ui`, may read one Key Vault secret and nothing else.
- **It plays the bank's login, for the demo only.** It signs short-lived login tokens for the
  synthetic customers with the demo identity provider's private key (`demo-idp-private-key` in
  Key Vault, uploaded with `make demo-idp-publish`). The intake API verifies those tokens exactly
  as it would a real one.
- **Progress is the stored business status**, polled every 0.5 s (ADR-0016), never a guess from
  which step is running. A dispute is shown as settled only when nothing more will change
  without a person; `refund_approved` is shown as "being paid".
- **The fast path is visible:** a dispute decided by a confirmed incident shows "⚡ Decided
  without a model: 0 model calls, $0", the incident and who confirmed it, and the policy checks
  it still passed. An agent investigation links to its Langfuse trace.
- **The dashboard shows how its two key numbers are built:** evaluated requests, successful
  requests, success rate (successful ÷ evaluated), total cost, and cost per success (total cost ÷
  successful), recomputed from the evaluation reports, per run over time. The reports are baked
  into the image when it is built.
- **No public address.** A ClusterIP Service, opened with `make ui` (a port-forward).
- Every decision the screen makes is in `services/demo_ui/logic.py`, tested against the real
  intake API in-process; the page itself is rendered headless in a test (Streamlit `AppTest`).

## Consequences
- + The demo exercises the real system end to end, with the same checks a customer would hit.
- + A UI bug cannot approve, pay, or read anything a customer could not.
- - Holding the demo identity provider's key means the UI can log in as any synthetic customer.
  That is its purpose here, and why it has no public address.
- - Progress is polled, so a status change appears up to 0.5 s late.
- - The dashboard shows the reports committed when the image was built, not live runs.

## Production delta
The customer's app logs in through the bank's identity provider and never holds a signing key;
progress is pushed (server-sent events or WebSockets) rather than polled; the operations
dashboard reads evaluation and production metrics from a metrics store, with access control;
an operations view lists disputes waiting for a person, with the evidence and a decision form.
