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
