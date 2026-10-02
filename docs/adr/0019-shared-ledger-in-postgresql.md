# ADR-0019: The ledger is one shared PostgreSQL store, and the database enforces "pay once"

- **Status:** Accepted (deployed)
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 11)

## Context
Core Systems runs as two replicas. Its ledger was held in each replica's memory, which was fine
while the ledger was only read: both replicas held the same starting data.

Step 11 adds the first write, a refund. With the ledger in memory, a refund paid by replica A is
unknown to replica B, so both protections against a double payment stop working:

| Request reaching replica B after replica A paid | In-memory ledger |
|---|---|
| The same idempotency key (a retry) | B has never seen the key: it pays again |
| A different key for the same transaction | B still sees the transaction as failed: it pays again |
| A read of the transaction | B answers "failed, money owed" |

A test shows this on purpose (`test_two_instances_of_core_systems`).

## Decision
- **The ledger lives in PostgreSQL**, shared by every replica: a new adapter,
  `services/core_systems/adapters/postgres.py`, behind the existing `LedgerRepository` port.
  The API did not change. `CORE_SYSTEMS_BACKEND=postgres` selects it.
- **The database enforces the rules itself**, so they hold whatever the code above it does:
  - `idempotency_key` is unique: one request is recorded once
  - `transaction_id` is unique in the refunds table: one refund per transaction, whatever the key
  - the refund row and the change to the transaction are written in one database transaction:
    both happen or neither does
  - the transaction's row is locked while the rules are checked, so two requests cannot both
    see "failed" and both pay
- **The same tests run against both adapters.** Every test in `tests/test_core_refunds.py` runs
  with the in-memory ledger and, under `make test-db`, with a real PostgreSQL.
- **Two records, two owners.** The dispute record (the supervisor's database) says where the
  customer's case stands. The ledger (Core Banking) says where the money is. The guarantee
  against paying twice is in the ledger, because it cannot rely on its callers behaving.
- Risk signals stay in memory: they are read-only reference data.

## Where it runs
The same PostgreSQL pod as the dispute records, with a **separate database and user**
(`make ledger-password`, `make ledger-db`):

| Database | Owner | Who may connect |
|---|---|---|
| `disputes` | `disputes` | the supervisor and the worker |
| `ledger` | `ledger` | Core Systems only (`id-core-systems` reads `ledger-password` and nothing else) |

A second pod would not fit comfortably in the 4 vCPU quota, and separate users already keep the
two systems' data and credentials apart.

## Verified on the cluster
A refund paid by one Core Systems pod, then the same request on the other pod:

| Request | Pod | Result |
|---|---|---|
| Key 1 | A | `201`, refund `RF-60a0...` |
| The same key | B | `200`, `Idempotent-Replay: true`, the same refund |
| A new key, same transaction | B | `422 already_refunded` |

One refund row in the ledger. The `ledger` user is refused a connection to `disputes`.

## Why not Redis
- **Our Redis forgets on restart.** It runs with no snapshots and no append-only file, on
  purpose: it is the fast path of the duplicate check (ADR-0015), and the worst case of losing
  it is a duplicate that PostgreSQL then refuses. A ledger that forgets a refund pays it again.
- **A refund is several changes that must happen together** (check the rules, record the
  refund, mark the transaction). PostgreSQL gives that as a transaction with constraints. In
  Redis we would write that logic ourselves, with nothing underneath to catch a mistake.
- **The Redis key answers a different question.** It says "a dispute for this customer and
  transaction is already open", and it expires. It says nothing about whether money moved.

The pattern is the same as at the gate: Redis for a fast answer, PostgreSQL for the guarantee.

## Consequences
- + A retry that lands on another replica returns the refund already made.
- + The guarantee survives restarts and code mistakes in the layers above.
- - Core Systems now depends on a database: it is not ready if PostgreSQL is unreachable.
- - The in-memory adapter remains for tests and single-process runs, and must not be used with
  more than one replica now that the ledger can be written to.

## Production delta
The ledger is the bank's core banking system, not a table we own: this adapter would be replaced
by a client for it, keeping the same port and the same idempotency key on the request. A
database of its own with its own credentials, separate from the dispute records; versioned
migrations instead of creating tables at startup; a double-entry ledger (a credit and a debit
row per movement) instead of a status column; a retention policy for idempotency keys.
