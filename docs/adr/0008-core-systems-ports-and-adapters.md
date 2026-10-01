# ADR-0008: Core Systems service built as production code, with swappable data adapters

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M2

## Context
The agents need a Core Banking API (transactions, refund history) and a Risk Engine API (fraud
signals). For the demo the data is synthetic, but we may later connect real systems. A "mock"
written as throwaway code would have to be rewritten, and the agents' tools would change with it.

## Decision
Build `services/core_systems` as if it were the real service; only the data source is synthetic.

- **Ports & adapters (hexagonal):** the API depends on `ports.LedgerRepository` and
  `ports.RiskRepository` (Python `Protocol`s). `adapters/in_memory.py` implements them over
  fixtures. A real adapter (PostgreSQL, HTTP client to core banking) is added without touching
  the API. `CORE_SYSTEMS_BACKEND` selects the adapter at startup; unknown values fail fast.
- **Versioned, resource-oriented API:** `/v1/core-banking/...` and `/v1/risk/...`, so the agents'
  tools target a stable contract.
- **Customer scoping at the data-access layer:** every port method takes `customer_id` and
  queries by it. Other customers' transactions return **404, not 403** (no enumeration).
- **Caller identity via `X-Customer-Id` header**, set by agents from the verified JWT (Step 3),
  never from LLM output. The API is ClusterIP-only.
- **Async ports**, because real adapters do network I/O.
- **Separate liveness (`/healthz`) and readiness (`/readyz`)**: readiness checks dependencies
  and removes the pod from the Service; liveness never does, to avoid restart storms when a
  dependency is down.
- **Runtime hardening:** non-root UID 10001, read-only root filesystem, all capabilities dropped,
  seccomp `RuntimeDefault`, no ServiceAccount token; namespace enforces Pod Security `restricted`.
  2 replicas spread across nodes, `maxUnavailable: 0` rolling updates.

## Consequences
- + Replacing synthetic data with a real system is one new adapter class.
- + Agents' tools are written once against `/v1`.
- - More files than a single-module mock.
- - Trusting `X-Customer-Id` is only safe because the API is internal; see production delta.

## Production delta
Service-to-service authentication (mTLS via a service mesh, or Entra ID tokens validated by the
API) and the agent forwards the user's token so core banking re-checks identity itself; network
policies so only the agent pods can reach this Service (requires a policy-enabled CNI, e.g.
Cilium); pinned base image digests and SBOM; OpenAPI published to API management.
