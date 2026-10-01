# ADR-0005: Azure Key Vault for secrets, mounted via Secrets Store CSI driver

- **Status:** Accepted (`jwt-signing-key` handling refined by ADR-0009)
- **Date:** 2026-10-01
- **Milestone:** M2

## Context
ADR-0001 removes secrets wherever Entra ID works (ARM, Azure OpenAI, ACR). Some credentials
cannot use Entra ID: Langfuse Cloud API keys and the demo JWT signing key (Step 3). Plain
Kubernetes Secrets are only base64-encoded, live in etcd, are readable by anyone with `get
secrets` in the namespace, and have no audit trail or rotation story.

## Decision
- **Vault:** one Key Vault (`Standard`) per environment, in **RBAC authorization mode**
  (not legacy access policies), so access is managed with the same Azure role assignments as
  everything else.
- **Humans:** the operator gets `Key Vault Secrets Officer` (write/read). Even the subscription
  Owner cannot read secret values until granted a data-plane role.
- **Workloads:** each service that needs a secret gets its own user-assigned managed identity,
  federated to its Kubernetes ServiceAccount (Workload Identity, ADR-0001), with
  `Key Vault Secrets User` (read-only) scoped to only the secrets it needs.
- **Delivery to pods:** AKS addon `azure-keyvault-secrets-provider` (Secrets Store CSI driver).
  Secrets are mounted as files in the pod; the app never holds a Key Vault credential.
- **Inventory:**

  | Secret | Consumer | Why not Entra ID |
  |---|---|---|
  | `langfuse-public-key`, `langfuse-secret-key` | supervisor (M5) | Third-party SaaS API key |
  | `jwt-signing-key` | UI token issuer, services verifying tokens (M3) | Demo identity provider |
  | ~~Azure OpenAI key~~ | — | Not needed: Entra ID via Workload Identity |

## Consequences
- + One audited place for secrets; access per workload and per secret.
- + Rotating a secret in Key Vault does not require rebuilding images.
- - Libraries that expect env vars (Langfuse SDK) need either a startup shim reading the mounted
  file, or CSI `secretObjects` sync into a Kubernetes Secret (which reintroduces an etcd copy).
  Decided in M5.
- - Soft delete is on (Azure default, 7-90 day retention) but purge protection is **off**, so the
  demo can be torn down and the name reused.

## Production delta
Purge protection on; private endpoint with public network access disabled; separate vaults per
environment; secret rotation enabled on the CSI driver (`--enable-secret-rotation`) with expiry
dates and Event Grid alerts on near-expiry; Defender for Key Vault; diagnostic logs to Log Analytics.
