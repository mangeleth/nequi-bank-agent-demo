# Roadmap

Every milestone ends **deployed to AKS** (namespace `disputes`) and verified (see ADR-0004).
Kubernetes manifests and tests are written alongside each service, not at the end.

| Milestone | Builds | Deployed at the end | Status |
|---|---|---|---|
| **M1** Cluster & identity | `Makefile`, `scripts/azure_setup.py` | AKS cluster, verified by `make aks-verify` | Done |
| **M2** Delivery pipeline + first service | ACR, Key Vault + CSI driver, `shared/schemas.py`, `services/core_systems/` (+ Dockerfile, manifests), `make release` | Core Banking + Risk Engine API (synthetic data adapter), reachable in-cluster | Done |
| **M3** Security boundary + Fraud Agent | `shared/auth.py`, `services/fraud_agent/`, Azure OpenAI + Workload Identity federation | Fraud Agent calling Core Systems and Azure OpenAI with no stored keys | Done |
| **M4** Ledger Agent | MCP server in Core Systems, `services/ledger_agent/` (MCP client) | Ledger Agent querying settlement state over MCP | Done |
| **M5** LangGraph supervisor, circuit breakers & tracing | `services/supervisor/` (LangGraph + Langfuse Cloud, keys from Key Vault); steps below | `/disputes/triage` orchestrating both agents, traces in Langfuse | In progress |
| **M6** Demo UI | `services/ui/` (Streamlit) | Nequi-style UI end to end | |
| **M7** Automated security & failure-mode tests, CI/CD | GitHub Actions (OIDC); the test suite as a merge and deploy gate; see below | Pushing to `main` builds, tests, and deploys automatically | |

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

## Milestone 7: the three-tier defensive barrier

Resilience in a multi-agent system needs three layers. All three exist today; Milestone 7 runs
them automatically on every change.

| Tier | What it is | Where |
|---|---|---|
| 1. Deterministic state counters (application-level circuit breaker) | `turns`, `ledger_calls`, `fraud_calls` in `TriageState`, incremented by code; `breaker()` sends the dispute to the `escalate` node at 6 supervisor turns or a third call to the same agent | `services/supervisor/graph.py` |
| 2. Engine recursion limit | `recursion_limit=15` on every run caps total super-steps and inference cost; `GraphRecursionError` becomes an escalation | `services/supervisor/main.py` |
| 3. Automated adversarial tests | A model that loops, agents that are down, slow, or answer with garbage, an inflated refund, a fooled model, a rogue MCP server: each must end gracefully in human review | `tests/test_supervisor.py`, `tests/test_supervisor_clients.py`, `tests/test_fraud_agent.py`, `tests/test_ledger_agent.py` |

Still to do in Milestone 7: run the suite in GitHub Actions as a required check, and add an
evaluation run of the seven fixture scenarios against the real model.
