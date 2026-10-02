# ADR-0022: Disputes covered by a confirmed incident are decided by code, without a model

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 12)

## Context
When the platform itself fails (an interbank link times out for 40 minutes), thousands of
customers have the same problem, and the bank already knows the answer. Running three agents
and eight model calls per dispute to rediscover it is slow, costly, and one more chance to be
wrong (docs/LEARNINGS.md, Part 2, entry A). The order of preference is: a database fact, then a
deterministic rule, then an agent for what remains ambiguous.

## Decision
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

- **The check runs in the worker, before the graph** (`services/supervisor/incident_path.py`),
  not in the intake API. The intake API is the customer-facing part and only stores and queues
  (ADR-0018); an incident is exactly the kind of burst the queue absorbs. It is still zero model
  calls, and no delegated token is issued.
- **It removes the investigation, not the controls.** A covered dispute goes through the same
  `evaluate()` as every other one. Only where three inputs come from changes:

  | Policy input | Normal path | Covered by an incident |
  |---|---|---|
  | Ledger figures | Ledger Agent (a model) | Core Banking, read by code |
  | Fraud risk | Fraud Agent (a model) | The risk engine's own score, read by code |
  | Recommendation | The supervisor model | Code, citing the incident; the amount is the ledger's |
  | Refund history | Code | Code |

  So the kill switch, the amount limit, the risk check, and the 30-day limits still apply; a test
  covers each.
- Covered but nothing owed (already refunded): closed without paying.
- The customer is told what is established: *"This transfer was affected by a confirmed problem
  on our side: <incident title>."*, then the usual message.
- If Core Systems does not answer the incident check, the delivery is retried. An outage is never
  read as "not covered", which would quietly spend model calls during an incident.

- **The batch refund** (`services/core_systems/incident_refunds.py`, `make incident-refunds`)
  pays every covered transaction, including those of customers who never disputed:
  - a **dry run by default**: it prints the plan; `--execute` (`EXECUTE=true`) pays
  - each refund goes through the ledger's own `execute_refund()`, with the same rules as any
    refund; the key is `incident:<incident>:<transaction>`, so running it again pays nothing more
  - `--max-total` refuses the whole run if the plan exceeds it, so a wrong window cannot
    quietly pay out a fortune
  - incident refunds do not count towards the customer's automatic-refund limits: the customer
    did not claim them
- **A dispute and the batch at the same moment pay once.** Their keys differ, so the key cannot
  help; the ledger's state does (the row is locked while the rules are checked, and one refund per
  transaction is a database constraint). A test races the two.
- **A dispute the batch already paid is recorded as paid.** The payer gets `already_refunded`,
  looks up the refund that exists (`GET /v1/core-banking/transactions/{id}/refund`), and records
  the dispute as `refund_paid` with it, instead of sending a paid customer to a person.

## Verified on the cluster
| Check | Result |
|---|---|
| The cluster's ledger, created before this step | Upgraded in place on start: columns backfilled, new rows and the incident added |
| Evaluation, 12 scenarios | 12 of 12 |
| `known-incident-fast-path` (TX-...0009, covered) | Paid; **0 model calls, $0, 0.2 s** |
| `incident-window-edge` (TX-...0011, after the window) | The agents investigated: 10.9 s, $0.0194 |
| Batch: dry run, execute, execute again | Planned TX-...0010 only (TX-...0009 already paid); paid it; then paid nothing |
| user-1003's refund history after the batch | Unchanged (3 refunds, 65,000 COP): the incident refund is not counted |
| user-1003 disputes TX-...0010 after the batch | Closed in 0.7 s without a model: "already returned"; still one refund |

## Production delta
Incidents are created from monitoring (a spike of one failure code), proposed to operations, and
confirmed with an audit record of who confirmed what; they live in the bank's incident system,
not in the ledger's database.
