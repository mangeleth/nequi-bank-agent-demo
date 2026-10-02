# ADR-0021: Approved refunds go through their own queue, paid by a separate payer at a fixed pace

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 11)

## Context
After ADR-0020 the triage worker paid a refund right after deciding it. That had three costs:
- **No control of pace.** A burst of approvals became a burst of writes to the bank's ledger, at
  whatever rate triage happened to finish.
- **No way to stop paying without stopping deciding.** During a ledger incident operations could
  only switch off approvals (the kill switch), not hold payments.
- **The process that runs the model could move money.** The worker held both the model and the
  path to the refund endpoint.

## Decision
- **A `refunds` queue.** When a refund is approved, the worker saves the decision and sends the
  dispute ID to `refunds`. It does not pay.
- **A `refund-payer` deployment** takes from `refunds` and pays (`services/supervisor/payer.py`,
  `payments.py`). Its identity, `id-refund-payer`, may receive from `refunds` and read the
  dispute database's password. It has no model access, no signing key, and no tracing keys.

  | Identity | `disputes` queue | `refunds` queue | Model | Signs tokens |
  |---|---|---|---|---|
  | `id-supervisor` (intake API) | send | – | – | – |
  | `id-triage-worker` | receive | send | yes | yes |
  | `id-refund-payer` | – | receive | – | – |

- **Pace:** at most `REFUND_PAYMENTS_PER_SECOND` payments start per second (default 2), and at
  most 4 are in flight. **One replica**, so the per-process pace is also the rate the ledger sees.
  If the payer dies, refunds wait in the queue until Kubernetes restarts it: delayed, never lost
  or paid twice. Deploys replace the pod without overlap (`maxSurge: 0`).
- **Pause:** `REFUND_PAYMENTS_PAUSED=true` stops paying. Approved refunds wait in the queue;
  triage keeps deciding. This is separate from the approval kill switch (ADR-0007).
- **More patience than triage:** the `refunds` queue allows 5 deliveries (dispute queue: 2), with
  a 1-minute lock and about 10 seconds (with jitter) between attempts. After the last delivery the
  dispute goes to a person with the idempotency key to look up, and the message is dead-lettered.
  A test checks that the code's limit equals the queue's setting in the Makefile.
- **Duplicates are harmless.** The payer only pays a dispute that is finished and still
  `refund_approved`; anything else is completed without action. The ledger pays a key only once.

## Verified on the cluster
| Test | Result |
|---|---|
| Evaluation through intake, both queues, worker, and payer | 10 of 10; one ledger refund per paid transaction; paid 0.8 s after the decision was saved |
| Pause: `REFUND_PAYMENTS_PAUSED=true`, then a dispute | Approved; 10 s later still `refund_approved`, "not been paid yet"; 1 message waiting in `refunds`; 0 ledger refunds |
| Unpause | Paid 0.4 s after the payer restarted; 1 ledger refund |

## Consequences
- + The ledger's write rate is a setting, not a side effect of how fast triage runs.
- + Payments can be held during an incident without stopping triage or losing work.
- + The process with model access no longer moves money.
- - Payment adds a queue hop, so `refund_paid` arrives a little after the decision.
- - One payer replica is a single point of delay (not of loss).
- - Basic tier has no scheduled redelivery, so "patience" is about 5 attempts over tens of seconds,
  not minutes of backoff.

## Production delta
**A shared limit for several payers.** With one replica the per-process pace is the ledger's rate.
With several, each counting on its own, the total multiplies (3 payers x 2 per second = 6). So
every payer asks one shared counter before each payment: a token bucket in Redis, or the core
banking system's own admission control.

If the shared counter is down, payers **wait (fail closed)**: a refund can wait safely in the
queue, but an overloaded core banking system affects every customer. For long outages, a
conservative fallback: each payer pays at a fixed low share of the limit, counted locally (below
limit / payers, since the number of payers can change).

Also: Standard tier with scheduled redelivery and exponential backoff,
so a ledger outage of minutes is waited out; an alert on queue depth and on the dead-letter queue;
the pause switch as an audited operations action rather than a config rollout; a network policy
so only the payer can reach the refund endpoint.
