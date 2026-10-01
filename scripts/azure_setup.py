"""Verify Azure identity, AKS control plane (ARM), and Kubernetes data plane access.

Checks run cheapest-first and stop at the first failure (see ADR-0002):
    1. identity   -> can we get an Entra ID token, and as whom?
    2. control    -> does ARM report the cluster as provisioned, running, and workload-identity ready?
    3. data plane -> can we reach the Kubernetes API server with the permissions we need?

Usage: make aks-verify   (reads settings from .env via the Makefile)
"""

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.containerservice.models import ManagedCluster

ARM_SCOPE = "https://management.azure.com/.default"
KUBECTL_TIMEOUT_S = 60


class CheckFailed(Exception):
    """A verification step failed; the message should tell on-call what to do next."""


@dataclass(frozen=True)
class AksTarget:
    subscription_id: str
    resource_group: str
    cluster_name: str
    namespace: str


def load_target_from_env() -> AksTarget:
    """Read AZURE_SUBSCRIPTION_ID, AKS_RESOURCE_GROUP, AKS_CLUSTER_NAME, K8S_NAMESPACE."""
    names = ["AZURE_SUBSCRIPTION_ID", "AKS_RESOURCE_GROUP", "AKS_CLUSTER_NAME", "K8S_NAMESPACE"]
    values = {name: os.environ.get(name, "").strip() for name in names}

    # Report every missing variable at once so the user fixes .env in a single pass.
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise CheckFailed(f"missing environment variables: {', '.join(missing)} (see .env.example)")

    return AksTarget(
        subscription_id=values["AZURE_SUBSCRIPTION_ID"],
        resource_group=values["AKS_RESOURCE_GROUP"],
        cluster_name=values["AKS_CLUSTER_NAME"],
        namespace=values["K8S_NAMESPACE"],
    )


def _decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload for display only. The signature is NOT verified: never use this for authorization."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # base64url drops padding; restore it
    return json.loads(base64.urlsafe_b64decode(payload))


def verify_identity(credential: TokenCredential) -> dict:
    """Get an ARM token and return who we are: {"tid": ..., "oid": ..., "name": ...}."""
    try:
        token = credential.get_token(ARM_SCOPE).token
    except ClientAuthenticationError as exc:
        raise CheckFailed(f"no Azure credential available - run `az login` ({exc.message.splitlines()[0]})") from exc

    claims = _decode_jwt_claims(token)
    # Humans carry upn/unique_name; service principals and managed identities only carry appid.
    name = claims.get("upn") or claims.get("unique_name") or claims.get("appid") or "<unknown>"
    return {"tid": claims.get("tid"), "oid": claims.get("oid"), "name": name}


def verify_cluster(credential: TokenCredential, target: AksTarget) -> ManagedCluster:
    """Fetch the cluster from ARM and check it is provisioned, running, and workload-identity ready."""
    client = ContainerServiceClient(credential, target.subscription_id)
    try:
        cluster = client.managed_clusters.get(target.resource_group, target.cluster_name)
    except ResourceNotFoundError as exc:
        raise CheckFailed(
            f"cluster {target.cluster_name} not found in {target.resource_group} - run `make aks-create`"
        ) from exc
    except HttpResponseError as exc:
        raise CheckFailed(f"ARM refused the request ({exc.status_code}) - check your Azure role on the cluster") from exc

    problems = []
    if cluster.provisioning_state != "Succeeded":
        problems.append(f"provisioning_state={cluster.provisioning_state} (last create/update did not finish)")
    power = cluster.power_state.code if cluster.power_state else None
    if power != "Running":
        problems.append(f"power_state={power} - run `make aks-start`")
    if not (cluster.oidc_issuer_profile and cluster.oidc_issuer_profile.enabled):
        problems.append("OIDC issuer disabled (needed for Workload Identity, ADR-0001)")
    wi = cluster.security_profile.workload_identity if cluster.security_profile else None
    if not (wi and wi.enabled):
        problems.append("Workload Identity disabled (ADR-0001)")

    if problems:
        raise CheckFailed("cluster not ready: " + "; ".join(problems))
    return cluster


def _kubectl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["kubectl", *args], capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S
        )
    except FileNotFoundError as exc:
        raise CheckFailed("kubectl not found - run `sudo az aks install-cli`") from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckFailed(f"kubectl timed out after {KUBECTL_TIMEOUT_S}s - API server unreachable?") from exc


def verify_data_plane(target: AksTarget) -> None:
    """Check the Kubernetes API server is reachable, all nodes are Ready, and we can deploy."""
    result = _kubectl("get", "nodes", "-o", "json")
    if result.returncode != 0:
        raise CheckFailed(f"cannot list nodes - run `make aks-creds` / `make aks-rbac` ({result.stderr.strip()})")

    nodes = json.loads(result.stdout)["items"]
    if not nodes:
        raise CheckFailed("cluster has no nodes")
    not_ready = [
        node["metadata"]["name"]
        for node in nodes
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
    ]
    # All-or-nothing: with only 2 nodes and 4/4 vCPU quota, losing one node means our workloads won't fit.
    if not_ready:
        raise CheckFailed(f"nodes not Ready: {', '.join(not_ready)}")

    # Prove the permission we actually need later (Step 10), not just read access.
    result = _kubectl("auth", "can-i", "create", "deployments", "-n", target.namespace)
    if result.stdout.strip() != "yes":
        raise CheckFailed(f"cannot create deployments in '{target.namespace}' - run `make aks-rbac`")

    print(f"      {len(nodes)}/{len(nodes)} nodes Ready; can create deployments in '{target.namespace}'")


def main() -> int:
    try:
        target = load_target_from_env()
        credential = DefaultAzureCredential()

        who = verify_identity(credential)
        print(f"[1/3] identity   OK  tenant={who['tid']} user={who['name']}")

        cluster = verify_cluster(credential, target)
        print(f"[2/3] control    OK  {cluster.name} k8s={cluster.current_kubernetes_version}")

        verify_data_plane(target)
        print("[3/3] data plane OK")
    except CheckFailed as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
