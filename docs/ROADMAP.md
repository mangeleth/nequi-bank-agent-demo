# Roadmap

Every milestone ends **deployed to AKS** (namespace `disputes`) and verified (see ADR-0004).
Kubernetes manifests and tests are written alongside each service, not at the end.

| Milestone | Builds | Deployed at the end | Status |
|---|---|---|---|
| **M1** Cluster & identity | `Makefile`, `scripts/azure_setup.py` | AKS cluster, verified by `make aks-verify` | Done |
| **M2** Delivery pipeline + first service | ACR, Key Vault + CSI driver, `shared/schemas.py`, `services/core_systems/` (+ Dockerfile, manifests), `make release` | Core Banking + Risk Engine API (synthetic data adapter), reachable in-cluster | Done |
| **M3** Security boundary + Fraud Agent | `shared/auth.py`, `services/fraud_agent/`, Azure OpenAI + Workload Identity federation | Fraud Agent calling Core Systems and Azure OpenAI with no stored keys | Done |
| **M4** Ledger Agent | MCP server in Core Systems, `services/ledger_agent/` (MCP client) | Ledger Agent querying settlement state over MCP | Done |
| **M5** LangGraph supervisor, circuit breakers & tracing | `services/supervisor/` (LangGraph + Langfuse Cloud, keys from Key Vault); steps below | Done |
| **M6** Safety gate: idempotency, a buffer for Core Banking, and a known-incident fast path | Deduplication key at the gate, a message queue for intake, a rate-limited approval drain, refund execution in Core Systems, an incident registry; steps below | Disputes accepted asynchronously; duplicates and known incidents never reach a model; the ledger is written at a controlled rate | In progress |
| **M7** Demo UI | `services/ui/` (Streamlit) | Nequi-style UI end to end | |
| **M8** Automated security & failure-mode tests, CI/CD | GitHub Actions (OIDC); the test suite as a merge and deploy gate; see below | Pushing to `main` builds, tests, and deploys automatically | |

Each milestone ships tests for what it adds (e.g. IDOR/parameter-injection tests land with
`shared/auth.py` in M3), and records its decisions as ADRs in `docs/adr/`.

## Milestone 5 steps

- **Step 7: `services/supervisor/graph.py`** — cyclic `StateGraph` supervisor configured with
  `AzureChatOpenAI`:
  - structured routing output (the supervisor picks the next step from a fixed set of values)
  - deterministic turn counters kept in graph state, incremented by code
  - a loop-breaking conditional edge that stops routing when a counter reaches its limit
  - a human-ops fallback escalation node for anything the graph cannot resolve
- **Step 8: `services/supervisor/main.py`** — FastAPI `/v1/disputes/triage` endpoint:
  - a hard `recursion_limit=15` on every graph run
  - instrumented with the Langfuse Cloud `CallbackHandler`

The supervisor's `DisputeVerdict` is a recommendation; the refund policy
(`shared/refund_policy.py`, ADR-0007) decides between automatic approval and human review.

## Milestone 6: the safety gate

Today a triage is one synchronous HTTP request, every request runs the models, and nothing is
ever written to the ledger (an `auto_approved` refund is a decision, not yet a payment).
Milestone 6 adds the gate that stands between customers, the agents, and the ledger.

**Idempotency and deduplication at the gate.** Frustrated customers tap "Dispute" 5-10 times.
- A deterministic key, `sha256(user_id, transaction_id)`, is computed right after the JWT is
  verified, before any model call and before any ledger write.
- The first request claims the key atomically in a store shared by all replicas. A duplicate
  never reaches a model: it gets the same dispute back (its current status, or the stored
  result once finished), so the customer sees one dispute, not an error.
- The same key travels with the refund to the ledger, so a retry can never pay twice.

**A buffer for the Core Banking engine.** The gate is a rate-limiting shock absorber.
- Disputes land in a message queue (Azure Service Bus; Kafka at larger scale) instead of
  holding an HTTP request open: `POST` returns `202 Accepted` with a dispute ID, and a status
  endpoint reports progress.
- Workers consume the queue and run the supervisor graph at a bounded concurrency, so 10,000
  simultaneous disputes become a queue depth, not 10,000 simultaneous agent runs and row locks.
- Approved refunds go to a second queue. The gate drains it at a rate the ledger can commit
  safely (bounded concurrency and requests per second) without exhausting connection pools,
  and retries with backoff; messages that keep failing go to a dead-letter queue for people.

**Known incidents bypass the agents.** When the platform already knows why a group of
transactions failed, an agent has nothing to investigate (`docs/LEARNINGS.md`, Part 2, entry A).
- Operations register an incident: transactions from a given source in a given time window
  failed for a verified reason (for example an ATM cluster with `ATM_DISPENSER_TIMEOUT`).
- The gate checks each incoming dispute against the incident registry before it reaches the
  queue. A match calls no model: the dispute goes straight to the deterministic refund path.
- A batch job refunds every affected transaction under the same idempotency keys, including
  those of customers who never filed a dispute.

Planned steps:
- **Step 9 (done):** dispute store and deduplication key in the supervisor (`claim`,
  `complete`, `release`) on Redis, with tests for 10 simultaneous identical requests (ADR-0015).
- **Step 10a (done, ADR-0016):** PostgreSQL dispute records, two separate statuses, `202
  Accepted`, and a status endpoint; processing starts at once inside the supervisor. The
  evaluation submits and polls: 10 of 10, accepted in about 0.3 s, result in about 9 s.
- **Step 10b (in progress):** the queue and a separate worker.
  - Done: the supervisor issues its own 2-minute token to act for a customer, valid for one
    transaction, signed by a key that never leaves Key Vault (ADR-0017). A customer's login token
    is no longer forwarded and never has to sit in a queue.
  - Next: the queue (at most 2 deliveries, then a dead-letter queue), the worker, and failure
    tests on the cluster (a killed worker, a poison message).
  Disputes are stored durably in **PostgreSQL** (a unique constraint on the dispute key; Redis
  stays in front as the fast path), with two separate statuses: an *execution status* for the
  run of the graph and a *business status* for the customer's dispute ([#12](https://github.com/mangeleth/nequi-bank-agent-demo/issues/12)).
- **Step 11:** refund execution in Core Systems (the first ledger write) with an idempotency
  key, and the rate-limited approval drain with a dead-letter queue.
- **Step 12:** known-incident fast path: an incident registry in Core Systems, the check at
  the gate, the batch refund job, and an evaluation scenario that proves a matching dispute
  makes zero model calls.
- Azure resources use Entra ID and Workload Identity, with no connection strings (ADR-0001).

## Milestone 7: the demo UI

- A Streamlit app simulating the Nequi app: submit a dispute, watch the investigation, read the
  outcome and the customer message, and open the Langfuse trace.
- **Seeing progress.** The backend consumes `graph.stream(..., stream_mode="updates")` to
  receive each node's update as it happens. Execution events are for monitoring the run;
  what the customer sees is the explicitly stored *business status* (Milestone 6, Step 10),
  never a guess from which node is running. With a checkpointer configured, the state of a
  dispute can also be inspected with `get_state`, and a human approval can pause and resume
  the graph.

## Milestone 8: the three-tier defensive barrier

Resilience in a multi-agent system needs three layers. All three exist today; Milestone 8 runs
them automatically on every change.

| Tier | What it is | Where |
|---|---|---|
| 1. Deterministic state counters (application-level circuit breaker) | `turns`, `ledger_calls`, `fraud_calls` in `TriageState`, incremented by code; `breaker()` sends the dispute to the `escalate` node at 6 supervisor turns or a third call to the same agent | `services/supervisor/graph.py` |
| 2. Engine recursion limit | `recursion_limit=15` on every run caps total super-steps and inference cost; `GraphRecursionError` becomes an escalation | `services/supervisor/main.py` |
| 3. Automated adversarial tests | A model that loops, agents that are down, slow, or answer with garbage, an inflated refund, a fooled model, a rogue MCP server: each must end gracefully in human review | `tests/test_supervisor.py`, `tests/test_supervisor_clients.py`, `tests/test_fraud_agent.py`, `tests/test_ledger_agent.py` |

Done early (during Milestone 5): the evaluation run against the real model. `make eval-cluster`
runs nine scenarios on the deployed system and reports task success, tool-call correctness,
numeric groundedness, latency, tokens, and **cost per success** (ADR-0014). Baseline: 9 of 9,
$0.0143 per success.

Still to do in Milestone 8: run the unit tests and the evaluation in GitHub Actions as required
checks; more scenarios, including conflicting and stale evidence (`docs/LEARNINGS.md`, Part 2,
entry C); several runs per scenario to measure decision agreement.

**A background LLM judge** ([#10](https://github.com/mangeleth/nequi-bank-agent-demo/issues/10)). Deterministic checks cannot tell whether an
explanation is supported by the evidence, so every finished triage is also judged:
- When a run finishes, a judging job is put on a queue (a Redis stream).
- A background worker takes the job and calls a judge model with the question, the candidate
  answer, the tool evidence of that run, and a rubric: **groundedness** (every factual claim has
  evidence), **completeness** (addresses the question or states what is unknown), **clarity**
  (understandable; extra length earns no credit). The candidate text and the evidence are
  treated as data, not instructions. The judge returns pass or fail per criterion with a brief
  reason.
- The verdict is stored in PostgreSQL next to the dispute, and the UI (Milestone 7) shows a
  table of disputes with their judge results.
- The judge runs after the customer has their answer: it measures quality and raises alerts;
  it does not block or change a decision.
- **Generalization and drift:** a held-out labelled set the rubric was never tuned on (does the
  judge reject an unsupported "bank rejection" after we fixed "insufficient funds"?), and the
  fixed calibration set re-run on a schedule and after every judge change. The UI shows a judge
  health panel: agreement and unsafe passes on the tuning set and the held-out set, over time.
- **Calibration** against answers labelled PASS or FAIL by people: report the agreement rate,
  the number of **unsafe passes** (a person said FAIL, the judge said PASS), and each
  disagreeing case, per rubric criterion. Re-run whenever the judge's prompt or model changes.

## Backlog (not scheduled)

| Issue | What |
|---|---|
| [#6](https://github.com/mangeleth/nequi-bank-agent-demo/issues/6) | Verify evidence timing: a decision can be made on a stale ledger reading |
| [#7](https://github.com/mangeleth/nequi-bank-agent-demo/issues/7) | Exercise and report a claimed amount that differs from the ledger |
| [#8](https://github.com/mangeleth/nequi-bank-agent-demo/issues/8) | Unresolved disputes should state what is known and what remains uncertain |
| [#9](https://github.com/mangeleth/nequi-bank-agent-demo/issues/9) | Compare retry policies by total spending and cost per success |
| [#11](https://github.com/mangeleth/nequi-bank-agent-demo/issues/11) | Diagnostics step: find the failure reason when the transaction record does not have one |
