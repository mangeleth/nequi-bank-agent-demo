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
| **M6** Safety gate: idempotency and a buffer for Core Banking | Deduplication key at the gate, a message queue for intake, a rate-limited approval drain, refund execution in Core Systems; steps below | Disputes accepted asynchronously; duplicates never reach a model; the ledger is written at a controlled rate | Next |
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

Planned steps:
- **Step 9:** dispute store and deduplication key in the supervisor (`claim`, `complete`,
  `release`), with tests for 10 simultaneous identical requests.
- **Step 10:** asynchronous intake: queue, worker, `202 Accepted`, and status endpoint.
- **Step 11:** refund execution in Core Systems (the first ledger write) with an idempotency
  key, and the rate-limited approval drain with a dead-letter queue.
- Azure resources use Entra ID and Workload Identity, with no connection strings (ADR-0001).

## Milestone 8: the three-tier defensive barrier

Resilience in a multi-agent system needs three layers. All three exist today; Milestone 8 runs
them automatically on every change.

| Tier | What it is | Where |
|---|---|---|
| 1. Deterministic state counters (application-level circuit breaker) | `turns`, `ledger_calls`, `fraud_calls` in `TriageState`, incremented by code; `breaker()` sends the dispute to the `escalate` node at 6 supervisor turns or a third call to the same agent | `services/supervisor/graph.py` |
| 2. Engine recursion limit | `recursion_limit=15` on every run caps total super-steps and inference cost; `GraphRecursionError` becomes an escalation | `services/supervisor/main.py` |
| 3. Automated adversarial tests | A model that loops, agents that are down, slow, or answer with garbage, an inflated refund, a fooled model, a rogue MCP server: each must end gracefully in human review | `tests/test_supervisor.py`, `tests/test_supervisor_clients.py`, `tests/test_fraud_agent.py`, `tests/test_ledger_agent.py` |

Still to do in Milestone 8: run the suite in GitHub Actions as a required check, and add an
evaluation run of the seven fixture scenarios against the real model. The evaluation reports
**cost per success**: the cost of all evaluated attempts, including retries, divided by the
number of disputes that ended in the expected outcome (see `docs/LEARNINGS.md`, Part 2).
