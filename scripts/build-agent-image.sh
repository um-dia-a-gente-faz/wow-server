#!/usr/bin/env bash
# Rebuild wow-agent:latest from the current checkout, or check that it is fresh.
#
#   scripts/build-agent-image.sh           rebuild the image and print what to do next
#   scripts/build-agent-image.sh --check   build nothing; exit 1 if the image is missing
#                                          or older than the last commit touching
#                                          agent/ or the Dockerfile (#131)
#
# Why: `docker compose -f docker-compose.agents.yml up -d agent-<name>` reuses an
# existing wow-agent:latest and never rebuilds it, and scripts/deploy.sh (the
# 5-minute auto-deploy) does not build this compose file either. A stale image
# started fine but its observability API (/healthz on 9601-9625) never bound.
# This script does not touch running containers or the worldserver.
set -euo pipefail

cd "$(dirname "$0")/.."
# The compose file requires AGENT_PASSWORD to parse; a build never uses it.
export AGENT_PASSWORD="${AGENT_PASSWORD:-build-only}"
COMPOSE=(docker compose -f docker-compose.agents.yml)
IMAGE=wow-agent:latest

image_created() { docker image inspect -f '{{.Created}}' "$IMAGE" 2>/dev/null; }

# Epoch of the last commit that changes what goes into the image. Empty when the
# checkout has no history for those paths (e.g. a shallow clone) — callers must
# treat that as "cannot determine", not as "fresh".
src_epoch() { git log -1 --format=%ct -- agent Dockerfile 2>/dev/null || true; }

if [ "${1:-}" = "--check" ]; then
  created=$(image_created) || { echo "STALE: $IMAGE does not exist; run scripts/build-agent-image.sh"; exit 1; }
  built=$(date -d "$created" +%s 2>/dev/null) || { echo "UNKNOWN: cannot parse docker Created '$created' for $IMAGE."; exit 1; }
  src=$(src_epoch)
  if [ -z "$src" ]; then
    echo "UNKNOWN: no git history for agent/ or Dockerfile (shallow clone?); cannot verify $IMAGE freshness."
    echo "         Run scripts/build-agent-image.sh to be sure."
    exit 1
  fi
  if [ "$built" -lt "$src" ]; then
    echo "STALE: $IMAGE built $created, but agent/ or Dockerfile changed $(date -d "@$src" -Iseconds)."
    echo "       Run scripts/build-agent-image.sh, then recreate the agent containers."
    exit 1
  fi
  echo "OK: $IMAGE built $created, not older than the last agent/ or Dockerfile change."
  exit 0
fi

# Every agent-* service shares this one image, so building any one of them is enough.
svc=$("${COMPOSE[@]}" --profile raid config --services | grep -m1 '^agent-')

echo "Building $IMAGE from $(git rev-parse --short HEAD) (service $svc) ..."
"${COMPOSE[@]}" build "$svc"

echo
echo "Built: $(docker image inspect -f '{{.Id}} created {{.Created}}' "$IMAGE")"
echo "Source: $(git log -1 --format='%h %cI %s' -- agent Dockerfile)"
echo
echo "Running containers keep the OLD image until they are recreated. For each agent:"
echo "  docker compose -f docker-compose.agents.yml up -d agent-<name>"
echo "Then confirm the observability API is bound (expect {\"ok\": true}):"
echo "  curl -s localhost:<AGENT_HTTP_PORT>/healthz        # 9601-9625"
