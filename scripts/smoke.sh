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

kubectl run "smoke-$(date +%s)" -n "$namespace" --rm -i --quiet --restart=Never --image="$image" \
  --overrides="{
    \"spec\": {
      \"securityContext\": {\"runAsNonRoot\": true, \"runAsUser\": 100, \"seccompProfile\": {\"type\": \"RuntimeDefault\"}},
      \"containers\": [{
        \"name\": \"smoke\", \"image\": \"$image\", \"args\": [$args],
        \"securityContext\": {\"allowPrivilegeEscalation\": false, \"capabilities\": {\"drop\": [\"ALL\"]}}
      }]
    }
  }"
echo
