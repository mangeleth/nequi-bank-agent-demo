# Architecture Decision Records

Each significant architectural decision is recorded as a short, immutable ADR.
To change a decision, add a new ADR that supersedes the old one, and mark the old one `Superseded by ADR-XXXX`.

| ADR | Title | Status | Milestone |
|-----|-------|--------|-----------|
| [0001](0001-workload-identity-no-secrets.md) | Use Entra ID / Workload Identity, not stored secrets | Accepted | M1 |
| [0002](0002-aks-cluster-baseline.md) | AKS cluster baseline for the demo | Accepted | M1 |
| [0003](0003-langfuse-cloud-no-pii-masking.md) | Langfuse Cloud tracing without PII masking | Accepted (demo only) | M5 |

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
