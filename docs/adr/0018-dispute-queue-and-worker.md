# ADR-0018: Disputes wait in a queue; a separate worker runs them, with at most two deliveries

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 10b)

## Context
After ADR-0016 a triage ran as a background task inside the supervisor process. That had three
weaknesses: a pod that died took its runs with it (a person had to pick each dispute up after
five minutes); a burst of disputes became a burst of simultaneous model calls, limited only per
pod; and the customer-facing process held every permission the triage needs (the model, the
signing key, tracing keys).

## Decision
- **A queue between accepting and working:** Azure Service Bus, Basic tier, with connection
  strings disabled (Entra ID logins only). The intake API stores the dispute, sends its ID, and
  answers `202`. A burst becomes queue depth instead of concurrent runs.
- **A message is only the dispute ID.** The worker reads the request and the customer from
  PostgreSQL, so the queue holds no customer data and no token.
- **Delivery rules belong to the broker**, not to our code: a 5-minute lock, and at most 2
  deliveries (one attempt and one retry), then the dead-letter queue. A worker that dies runs
  no code, so the rule that recovers its message cannot depend on code: the lock expires and the
  broker offers the message again.
- **The worker** (`services/supervisor/worker.py`, the `triage-worker` deployment) handles one
  delivery as: load the dispute; `queued -> running`, or take over a run still marked running
  on a second delivery; issue the delegated token (ADR-0017); run the graph; `running ->
  finished`; complete the message.
- **Failure handling:**
  - not the last delivery: wait a base delay with jitter (50-150%), then abandon; the broker
    redelivers
  - the last delivery: the dispute goes to a person (`failed` / `pending_human_approval`) and
    the message to the dead-letter queue
  - hitting the graph's recursion limit is not retried: it is a loop, and goes to a person
  - a dispute that cannot be queued at intake is stored and goes to a person; the customer
    still gets `202` and cannot create a second one
- **Safe to repeat.** A status changes only from the status it is expected to be in, and a run
  never starts more often than the delivery limit, so a message delivered twice cannot finish a
  dispute twice. A stray duplicate message is completed without running anything.
- **Bounded concurrency:** each worker runs at most 4 triages and takes a message only when a
  slot is free (no prefetch), so it never holds locks on work it has not started.
- **Shutdown:** on a deploy the worker stops taking messages and gets 30 seconds to finish runs
  in progress; Kubernetes waits 60. Unfinished runs were never completed, so they are redelivered.
- **Two identities instead of one:**

  | Identity | May | May not |
  |---|---|---|
  | `id-supervisor` (intake API, reachable by customers) | send to the queue; read the database password | receive from the queue, call a model, sign a token, read tracing keys |
  | `id-triage-worker` (no Service, not reachable) | receive from the queue, call the model, sign tokens, read its secrets | send to the queue |

- **Readiness of the intake API no longer depends on the agents.** If they are down, disputes
  are still accepted and wait in the queue.
- Port and adapters as elsewhere: `DisputeQueue` with an in-memory adapter (tests and local
  runs, where the worker runs inside the API process) and the Service Bus adapter, which has its
  own tests against the real service on a separate queue.

## Consequences
- + A worker can die at any point and the dispute is still finished by another, without a person.
- + The exposed process can add work to the queue and nothing else.
- + Workers scale with queue depth, the API with request rate.
- - A dispute whose worker died waits up to 5 minutes for the lock to expire.
- - Storing the dispute and sending the message are two steps. If the send fails the dispute goes
  to a person; a process dying exactly between them leaves a queued dispute that the sweeper
  sends to a person after 15 minutes.
- - Basic tier has no scheduled delivery, so the retry delay is the worker holding the message
  for a second or two before abandoning it, not a broker-side backoff.
- - One more Azure dependency in the request path: if Service Bus is unreachable the intake API
  reports not ready.

## Production delta
Transactional outbox so the status change and the message are written together; Standard or
Premium tier for duplicate detection, scheduled retries with exponential backoff, and private
endpoints; autoscaling of workers on queue depth (KEDA); an alert and a runbook for the
dead-letter queue, with a tool to replay a message after a fix; lock renewal for long runs;
a second queue for approved refunds, drained at a controlled rate (Step 11).
