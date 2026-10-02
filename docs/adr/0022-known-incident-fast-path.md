# ADR-0022: Disputes covered by a confirmed incident are decided by code, without a model

- **Status:** In progress (part 1 of 4: the incident registry)
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 12)

## Context
When the platform itself fails (an interbank link times out for 40 minutes), thousands of
customers have the same problem, and the bank already knows the answer. Running three agents
and eight model calls per dispute to rediscover it is slow, costly, and one more chance to be
wrong (docs/LEARNINGS.md, Part 2, entry A). The order of preference is: a database fact, then a
deterministic rule, then an agent for what remains ambiguous.

## Decision (so far)
- **An incident registry in Core Systems.** An incident is confirmed by a person in operations,
  once, and covers every transaction that failed with its `failure_code` towards its
  `recipient_bank` inside its time window. Transactions now carry `recipient_bank` and
  `failure_code` from the payment network.
- **The rule is narrow on purpose.** Same failure code AND same bank AND inside the window.
  A transfer to that bank that succeeded, or that failed after the window, is not covered and
  goes through the normal agent path.
- **Two endpoints, neither an MCP tool:**
  - `GET /v1/incidents/covering/{transaction_id}`: customer-scoped; another customer's covered
    transaction returns the same 404 as an uncovered one
  - `GET /v1/incidents/{incident_id}/transactions`: for the batch refund job (operations)
- The ledger implements the registry, because incidents are matched against its transactions.
  The in-memory adapter (`covers()`) and the PostgreSQL adapter (SQL) apply the same rule, and
  the same tests run against both.
- A ledger created before this step is upgraded in place when Core Systems starts: the two new
  columns are added and backfilled, missing rows are inserted, and existing state (a refund
  already paid) is not changed.

## Still to do
2. The check at the gate: a covered dispute is decided without calling a model.
3. The batch refund job for every covered transaction, including undisputed ones.
4. An evaluation scenario proving zero model calls, and the deploy.

## Production delta
Incidents are created from monitoring (a spike of one failure code), proposed to operations, and
confirmed with an audit record of who confirmed what; they live in the bank's incident system,
not in the ledger's database.
