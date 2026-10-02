# ADR-0016: Disputes are stored records in PostgreSQL, with separate execution and business statuses

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 10a)

## Context
A triage was one HTTP request that returned when the models had finished (5-15 seconds), and
the only memory of a dispute was a Redis key that expires. Three problems followed:
- A dispute outlives a run: a person approves later, a payment is confirmed later, a run may be
  retried. Nothing recorded the dispute itself.
- One status was doing two jobs. An automatically approved refund was reported as `resolved`
  although nothing had been paid (issue #12).
- If Redis restarted, a duplicate dispute could be created.

## Decision
- **A `disputes` table in PostgreSQL** is the durable record: the request, both statuses, the
  customer message, the result, timestamps. PostgreSQL because disputes are records about money
  that need a unique constraint, conditional updates, and queries by status (the human review
  queue is `WHERE business_status = 'pending_human_approval' ORDER BY created_at`).
- **Two statuses, stored separately:**
  - *Execution status* (queued, running, finished, failed): what happened to the run. For
    engineers and operations.
  - *Business status* (received, investigating, pending human approval, refund approved, refund
    paid, closed without refund, rejected): where the customer's dispute stands. Every value is
    a fact that has happened; there is no "resolved". An approved refund is `refund_approved`
    until the ledger confirms payment (Step 11).
- **Asynchronous API:** `POST /v1/disputes` stores the dispute and answers `202 Accepted` with a
  `Location`; `GET /v1/disputes/{id}` returns its current state. The customer message is chosen
  by code for every status, including "received" and "investigating".
- **The database is the guarantee, Redis the fast path:** `dispute_key` has a unique constraint.
  A duplicate that reaches the database (Redis restarted, key expired) is answered with the
  existing dispute instead of creating a second one.
- **A status changes only from the status it is expected to be in** (`UPDATE ... WHERE
  execution_status = ...`), so two workers cannot both start or finish the same run, without
  any lock in application code (optimistic concurrency).
- **Every change is appended to `dispute_events`** in the same statement: the audit trail.
- **Reads are scoped by customer in the query**; another customer's dispute is "not found".
- **Runs that never finish are failed by code:** a sweeper marks any run with no status change
  for 5 minutes as `failed`, and the dispute goes to a person with the note "run did not finish
  in time". The customer sees "marked for review", never "failed".
- **Bounded concurrency:** at most 4 triages run at once per replica; the rest wait.
- **Deployment:** PostgreSQL as a StatefulSet with a 1 GiB managed disk. Its password is
  generated straight into Key Vault and mounted by the CSI driver into the PostgreSQL pod and
  the supervisor, each with its own identity that may read only that secret. Port and adapters
  as elsewhere: an in-memory adapter for tests, the same test suite against both.

## Consequences
- + The customer gets an answer in about 0.3 seconds and can watch progress.
- + Status and customer message can no longer disagree about whether money has moved.
- + An auditor can read the history of a dispute from a table.
- - Runs execute inside the supervisor process. If that pod dies mid-run, the run is lost and a
  person picks the dispute up after 5 minutes. Step 10b moves runs to a queue that redelivers.
- - One PostgreSQL pod: no replica, no backups, no TLS inside the cluster. A password is one
  more secret (the only way to authenticate to an in-cluster PostgreSQL).
- - The schema is created at startup with `CREATE TABLE IF NOT EXISTS`; there is no migration
  tool yet, so changing a column needs one.

## Production delta
Azure Database for PostgreSQL with Entra ID authentication (no password), zone-redundant, with
point-in-time restore and TLS; a migration tool with reviewed, versioned migrations; the status
change and the outgoing queue message written together (transactional outbox); row-level
security by customer; retention and anonymisation rules for dispute data.
