# ADR-0004: Azure Container Registry and per-milestone delivery to AKS

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M2

## Context
The original roadmap built every service first and wrote Kubernetes manifests at the end (M7).
That defers all integration risk (image builds, networking, identity, config) to the last step.
We want every milestone to end with something running on AKS ("walking skeleton"), so problems
surface early and each milestone is demoable on its own.

## Decision
- **Registry:** one Azure Container Registry, `Basic` SKU, in the same resource group and region
  as the cluster. Admin user stays **disabled**.
- **Pull auth:** `az aks update --attach-acr` grants the cluster's kubelet managed identity
  `AcrPull` on the registry. No `imagePullSecrets`, no registry passwords (consistent with ADR-0001).
- **Push auth:** developers push with `az acr login` (Entra ID token, short-lived).
- **Build:** local `docker build` + `docker push` for now. ACR Tasks (`az acr build`) are often
  blocked on new/trial subscriptions, and local builds work offline from Azure quotas.
- **Tags:** immutable, `<service>:<git-short-sha>`. Never deploy `:latest`, so every running pod
  maps to an exact commit and rollback is "deploy the previous SHA".
- **Definition of done per milestone:** image(s) pushed, manifests applied to namespace
  `disputes`, rollout healthy, and a smoke check passes.

## Consequences
- + Integration issues found in M2, not M7; every milestone is a live demo.
- + Image -> commit traceability; trivial rollback.
- - Builds depend on a developer machine with Docker (x86_64 to match AKS nodes).
- - Basic SKU: no geo-replication, private endpoints, or content trust.

## Production delta
Premium ACR with private endpoint and no public access; images built and pushed by CI
(GitHub Actions using OIDC federated credentials, no stored secrets), signed (Notation) and
vulnerability-scanned (Defender for Containers); admission policy (Azure Policy / Ratify) only
admits signed images from the approved registry; GitOps (Flux) instead of `kubectl apply`.
