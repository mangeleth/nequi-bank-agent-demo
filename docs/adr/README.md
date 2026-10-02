# Architecture Decision Records

Each significant architectural decision is recorded as a short, immutable ADR.
To change a decision, add a new ADR that supersedes the old one, and mark the old one `Superseded by ADR-XXXX`.

| ADR | Title | Status | Milestone |
|-----|-------|--------|-----------|
| [0001](0001-workload-identity-no-secrets.md) | Use Entra ID / Workload Identity, not stored secrets | Accepted | M1 |
| [0002](0002-aks-cluster-baseline.md) | AKS cluster baseline for the demo | Accepted | M1 |
| [0003](0003-langfuse-cloud-no-pii-masking.md) | Langfuse Cloud tracing without PII masking | Accepted (demo only) | M5 |
| [0004](0004-acr-incremental-delivery.md) | Azure Container Registry and per-milestone delivery to AKS | Accepted | M2 |
| [0005](0005-key-vault-secrets.md) | Azure Key Vault for secrets, mounted via Secrets Store CSI driver | Accepted | M2 |
| [0006](0006-pydantic-contracts.md) | Pydantic v2 contracts as the trust boundary for LLM output | Accepted (amended by 0007) | M2 |
| [0007](0007-tiered-refund-approval.md) | Tiered refund approval: deterministic auto-approval for small, clear cases | Accepted | M2 |
| [0008](0008-core-systems-ports-and-adapters.md) | Core Systems service built as production code, with swappable data adapters | Accepted | M2 |
| [0009](0009-caller-identity-from-verified-jwt.md) | Caller identity comes only from a verified, asymmetrically signed JWT | Accepted | M3 |
| [0010](0010-model-choice-and-determinism.md) | gpt-4o at temperature 0, pinned and replaceable | Accepted | M3 |
| [0011](0011-fraud-agent-design.md) | Fraud Agent: the model chooses what to look up, code decides for whom | Accepted | M3 |
| [0012](0012-ledger-agent-over-mcp.md) | Ledger Agent reaches Core Banking through MCP, and its figures are verified in code | Accepted | M4 |
| [0013](0013-supervisor-graph-circuit-breakers-tracing.md) | Supervisor as a cyclic LangGraph with code-enforced circuit breakers, traced to Langfuse | Accepted | M5 |
| [0014](0014-evaluation-against-the-real-model.md) | Evaluate the deployed system against the real model, scored from its traces | Accepted | M8 |
| [0015](0015-deduplication-gate.md) | Deduplication gate: one dispute per customer and transaction | Accepted | M6 |
| [0016](0016-dispute-store-and-two-statuses.md) | Disputes are stored records in PostgreSQL, with separate execution and business statuses | Accepted | M6 |
| [0017](0017-token-exchange-for-delegated-work.md) | The supervisor issues its own short-lived token to act for a customer | Accepted | M6 |
| [0018](0018-dispute-queue-and-worker.md) | Disputes wait in a queue; a separate worker runs them, with at most two deliveries | Accepted | M6 |
| [0019](0019-shared-ledger-in-postgresql.md) | The ledger is one shared PostgreSQL store, and the database enforces "pay once" | Accepted | M6 |
| [0020](0020-paying-approved-refunds.md) | The worker pays approved refunds from the saved decision; definite refusals go to a person | Accepted | M6 |
| [0021](0021-refund-payer-and-refunds-queue.md) | Approved refunds go through their own queue, paid by a separate payer at a fixed pace | Accepted | M6 |
| [0022](0022-known-incident-fast-path.md) | Disputes covered by a confirmed incident are decided by code, without a model | Accepted | M6 |
| [0023](0023-demo-ui.md) | A Streamlit demo UI that uses the system only as a customer's app would | Accepted | M7 |
| [0024](0024-public-demo-ui.md) | The demo UI is published on the internet, open to anyone, with no sign-in | Accepted | M7.5 |
| [0025](0025-ui-style-and-traces.md) | A Nequi-inspired look, clearly labelled as independent, and traces drawn inside the demo | Accepted | M7.5 |

## Template

```markdown
# ADR-XXXX: <decision title>

- **Status:** Proposed | Accepted | Superseded by ADR-YYYY
- **Date:** YYYY-MM-DD
- **Milestone:** MX

## Context
What forces are at play? What problem are we solving?

## Decision
What we decided, stated plainly.

## Consequences
What becomes easier, what becomes harder, what we accept as risk.

## Production delta
What we would do differently for the real Nequi production system.
```
