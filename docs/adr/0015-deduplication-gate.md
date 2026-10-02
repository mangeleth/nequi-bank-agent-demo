# ADR-0015: Deduplication gate, one dispute per customer and transaction

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 9)

## Context
Frustrated customers tap "Dispute" 5-10 times. Each tap was a full triage: eight model calls,
about $0.014, 5-15 seconds, and a separate dispute for a human to reconcile. Once refunds are
executed (Step 11), a duplicate is also a path to paying twice. The supervisor runs as two
replicas, so the memory of "this dispute already exists" cannot live inside one process.

## Decision
- **Key:** `sha256(user_id, transaction_id)`, computed right after the JWT is verified. Reason
  and description are not part of the key: one customer has one dispute per transaction.
  The user ID comes from the verified token, so a customer cannot collide with, or probe for,
  another customer's dispute.
- **Position:** the gate is the first thing after authentication, before the ownership lookup
  and before any model call.
- **Semantics:** the first request claims the key and runs. A duplicate is answered at the gate:
  - the first is still running: `409` with `Retry-After`, "this dispute is already being processed"
  - the first has finished: `200` with the stored `TriageResult` and the header
    `Idempotent-Replay: true` (the same `dispute_id`)

  A duplicate gets an answer rather than being dropped: the customer sees one dispute, not an
  error or silence.
- **Store:** Redis, one small pod in the cluster. The claim is one atomic command,
  `SET key in_progress NX EX 120`, so of ten simultaneous requests on any replicas exactly one
  wins. A finished result replaces the marker with a 24-hour expiry. The image is imported into
  our registry so the cluster pulls only from ACR.
- **Releasing the key:** a request that is refused (404, not the caller's transaction) or cannot
  start (Core Systems down) gives the key back, so the customer can retry. A triage that ran,
  including one that ended in escalation, keeps its result: it is with a person, and a retry
  must not start a second one.
- **A crashed run** frees its key when the 120-second lock expires.
- **Fail closed:** if Redis cannot be reached, intake answers `503` and no triage runs. Redis is
  configured `noeviction`, so a full store refuses new claims instead of forgetting old ones,
  and the supervisor reports not ready without it.
- **Port and adapters:** `DisputeGate` with a Redis adapter (cluster) and an in-memory adapter
  (tests, local single process), selected by `DEDUP_BACKEND`.

## Consequences
- + Ten taps cost one triage. Duplicates take milliseconds and call no model.
- + The same key is ready to travel with the refund to the ledger (Step 11).
- - Redis is a new dependency in the request path: when it is down, no dispute is accepted.
- - One Redis pod without persistence: a restart forgets claims and results, so duplicates
  within the following 24 hours are judged afresh. Acceptable until refunds are executed; the
  ledger's own idempotency key is the final guard against paying twice.
- - Redis has no password, and this cluster does not enforce network policies, so any pod in
  the cluster can read stored results. Accepted for synthetic data.
- - A triage that takes longer than the lock (120 s) would let a duplicate start a second run.

## Production delta
Azure Managed Redis with Entra ID authentication and a private endpoint, zone-redundant; network
policies so only the supervisor reaches the store; the durable record of disputes in a database
with a unique constraint on the key, with Redis as the fast path in front of it; with
asynchronous intake (Step 10), Azure Service Bus duplicate detection using the same key as the
message ID; a lock heartbeat for long-running triages.
