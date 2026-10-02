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
| **M7** CI/CD & hardening | GitHub Actions (OIDC), boundary + e2e test gates | Pushing to `main` builds, tests, and deploys automatically | |

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
