#!/usr/bin/env bash
# Smoke-test a deployed service from INSIDE the cluster, through its ClusterIP Service DNS name.
# The throwaway curl pod satisfies the namespace's "restricted" Pod Security policy.
# Usage: scripts/smoke.sh <namespace> <url> [extra curl arguments...]
#   scripts/smoke.sh disputes http://core-systems/healthz
#   scripts/smoke.sh disputes http://core-systems/v1/... -H "X-Customer-Id: user-1001"
set -euo pipefail

namespace="$1"
url="$2"
shift 2
image="curlimages/curl:8.11.1"
pod="smoke-$(date +%s)"
trap 'kubectl delete pod "$pod" -n "$namespace" --ignore-not-found --wait=false >/dev/null' EXIT

# Build the pod spec with a JSON encoder so headers and request bodies need no manual escaping.
overrides=$(python3 - "$image" "$url" "$@" <<'EOF'
import json, sys
image, url, *extra = sys.argv[1:]
print(json.dumps({"spec": {
    "securityContext": {"runAsNonRoot": True, "runAsUser": 100, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{
        "name": "smoke", "image": image,
        "args": ["-sS", "--fail-with-body", "--max-time", "60", *extra, url],
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }],
}}))
EOF
)

kubectl run "$pod" -n "$namespace" --restart=Never --image="$image" --overrides="$overrides" >/dev/null

# Wait for curl to finish (not just start), then print its output once.
for _ in $(seq 90); do
  phase=$(kubectl get pod "$pod" -n "$namespace" -o jsonpath='{.status.phase}')
  [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
  sleep 1
done
kubectl logs "$pod" -n "$namespace"
echo
[[ "$phase" == "Succeeded" ]]
