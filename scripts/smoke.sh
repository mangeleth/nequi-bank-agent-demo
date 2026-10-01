#!/usr/bin/env bash
# Smoke-test a deployed service from INSIDE the cluster, through its ClusterIP Service DNS name.
# The throwaway curl pod satisfies the namespace's "restricted" Pod Security policy.
# Usage: scripts/smoke.sh <namespace> <url> [header]
set -euo pipefail

namespace="$1"
url="$2"
header="${3:-}"
image="curlimages/curl:8.11.1"
args='"-sS", "--fail-with-body", "--max-time", "10"'
[[ -n "$header" ]] && args+=", \"-H\", \"$header\""
args+=", \"$url\""

pod="smoke-$(date +%s)"
trap 'kubectl delete pod "$pod" -n "$namespace" --ignore-not-found --wait=false >/dev/null' EXIT

kubectl run "$pod" -n "$namespace" --restart=Never --image="$image" --overrides="{
    \"spec\": {
      \"securityContext\": {\"runAsNonRoot\": true, \"runAsUser\": 100, \"seccompProfile\": {\"type\": \"RuntimeDefault\"}},
      \"containers\": [{
        \"name\": \"smoke\", \"image\": \"$image\", \"args\": [$args],
        \"securityContext\": {\"allowPrivilegeEscalation\": false, \"capabilities\": {\"drop\": [\"ALL\"]}}
      }]
    }
  }" >/dev/null

# Wait for curl to finish (not just start), then print its output once.
for _ in $(seq 60); do
  phase=$(kubectl get pod "$pod" -n "$namespace" -o jsonpath='{.status.phase}')
  [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
  sleep 1
done
kubectl logs "$pod" -n "$namespace"
echo
[[ "$phase" == "Succeeded" ]]
