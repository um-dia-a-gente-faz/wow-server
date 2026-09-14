#!/usr/bin/env bash
# Redeploy the wow-server VM (192.168.1.64) from the current state of `main`.
#
# Run directly on the VM (or via `ssh root@192.168.1.64 /opt/wow-server/scripts/deploy.sh`):
#
#   cd /opt/wow-server && ./scripts/deploy.sh
#
# Fast-forwards the checkout to origin/main, then recreates any containers
# whose image, build context, or compose file changed. Both the game stack
# (docker-compose.yml) and the monitoring stack (monitoring/docker-compose.yml)
# live in this same checkout but run as separate compose projects, so this is
# safe to run even if one stack needs a rebuild and the other doesn't.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== fetching origin/main =="
git fetch origin
git reset --hard origin/main

echo "== game stack =="
docker compose up -d --build

echo "== monitoring stack =="
docker compose -f monitoring/docker-compose.yml up -d --build

echo "== status =="
docker compose ps
docker compose -f monitoring/docker-compose.yml ps
