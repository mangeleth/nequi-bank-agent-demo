# ADR-0001: Use Entra ID / Workload Identity, not stored secrets

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M1

## Context
Services must authenticate to Azure (ARM, Azure OpenAI). In a FinTech context, long-lived
client secrets are a recurring source of leaks (logs, images, repos) and require rotation.
The same code must run on a developer laptop and inside AKS pods.

## Decision
- All Azure SDK clients authenticate with `azure.identity.DefaultAzureCredential`.
- Locally it resolves to `AzureCliCredential` (`az login`); in AKS it resolves to
  `WorkloadIdentityCredential` (federated Kubernetes service-account token -> Entra ID token).
- No Azure client secrets are stored in the repo, images, or Kubernetes Secrets.
- Tooling logs the resolved identity (`tid`, `oid`, `upn`/`appid` claims from the access token)
  so a wrong-identity resolution is visible.

## Consequences
- + Same code path in dev and cluster; no secret rotation.
- + Every Azure call is attributable to a named identity in Entra ID audit logs.
- - The credential chain is implicit: a stray `AZURE_CLIENT_SECRET` env var silently wins.
- - Probing IMDS locally can add latency on failure.

## Production delta
Pin the credential explicitly in workloads (`WorkloadIdentityCredential`, or
`DefaultAzureCredential(exclude_*_credential=True)`) for deterministic resolution.
Third-party keys that cannot use Entra ID (e.g. Langfuse) go to Azure Key Vault via the
Secrets Store CSI driver instead of plain Kubernetes Secrets.
