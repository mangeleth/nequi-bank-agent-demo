# ADR-0002: AKS cluster baseline for the demo

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M1

## Context
No cluster exists yet. We need a cost-conscious AKS cluster that still demonstrates a
bank-grade security posture, hosting ~5 small services (ui, supervisor, fraud-agent,
ledger-agent, mock-core-systems) on internal ClusterIP networking.

## Decision
| Setting | Value | Rationale |
|---|---|---|
| Control-plane tier | `--tier free` | No SLA needed for a demo |
| Node pool | 2 x `Standard_D2s_v6` (2 vCPU / 8 GiB each) | Two nodes show pods spread across machines; 8 GiB leaves room after system pods. v5 was originally chosen but is not offered to this subscription in `eastus2` (see Consequences) |
| Identity | `--enable-oidc-issuer --enable-workload-identity` | Required by ADR-0001 |
| Kubernetes AuthN/Z | `--enable-aad --enable-azure-rbac --disable-local-accounts` | No shared admin certificate; every API call tied to a human/workload identity |
| Cost control | `az aks stop` / `az aks start` between sessions | Pay for nodes only while practising |
| Region | `eastus2` | Broadest Azure OpenAI catalog, first to receive new models/features, best documented; same region as Azure OpenAI avoids cross-region hops |

Connectivity is verified in this order (fail fast, cheapest first):
identity token -> ARM `ManagedCluster` (provisioning/power state, OIDC, workload identity) ->
kubeconfig via `kubelogin` -> API server (`kubectl auth can-i ... -n disputes`, nodes `Ready`).

## Consequences
- + Strong identity story with minimal cost.
- - `--disable-local-accounts` means losing Entra ID access locks you out (no break-glass admin cert).
- - Free tier has no uptime SLA.
- - VM SKU availability is restricted per subscription, independently of quota: `az aks create`
  rejected `Standard_D2s_v5` in `eastus2` ("VM size ... is not allowed in your subscription"),
  while v6/v7 sizes were allowed. Check with `az vm list-skus -l <region> --size <sku>` before choosing.
- - Subscription quota is 4 vCPU per region; 2 x D2s_v6 uses 4/4, so there is no headroom for
  surge nodes and cluster upgrades will fail until quota is raised.
- - `eastus2` adds ~20-40 ms latency vs `brazilsouth` for Colombian users and stores data outside
  South America (acceptable: synthetic data, see ADR-0003).

## Alternatives considered
- `brazilsouth`: closest to Colombia and keeps data in-continent; offered the same core models
  at decision time, but fewer niche models and less documentation coverage.
- 1 x `Standard_D4s_v5`: simpler, but a single node shows no distribution or redundancy.
- 2 x `Standard_B2s`: cheapest, but 4 GiB per node is tight once system pods are scheduled.

## Production delta
Standard/Premium tier with uptime SLA, availability zones, private cluster (no public API
server), Azure CNI + network policies, separate system/user node pools, autoscaling, and
a break-glass procedure for Entra ID outages.
