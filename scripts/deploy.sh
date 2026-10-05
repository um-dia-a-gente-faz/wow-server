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
# live in this same checkout but run as separate compose projects.
#
# Path-aware: only the stacks whose files changed between HEAD and origin/main
# are touched (game: docker-compose.yml, Dockerfile, tdb/, tools/chat-feed;
# monitoring: monitoring/, exporters/, tools/wowmap, tools/dbc). A docs, agent
# or test merge touches neither. Before the game stack, the script defers (exit 0,
# checkout untouched, so the next poll retries) while any character is online.
#
# Env: FORCE=1 deploy despite players online · ALL=1 deploy both stacks
#      regardless of the diff · DRY_RUN=1 print the plan, change nothing ·
#      METRICS_DIR (default /opt/wow-server-metrics) gets a deploy.log line.
#
# Secrets (DB root password, web UI password) are not tracked: both stacks
# interpolate them from the gitignored .env next to docker-compose.yml. Copy
# .env.example to .env and fill it in once per checkout (docs/DEPLOYMENT.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# stacks_for: stdin = changed paths, stdout = "game" and/or "monitoring".
stacks_for() {
  local f game=0 mon=0
  while IFS= read -r f || [ -n "$f" ]; do
    case "$f" in
      docker-compose.yml|Dockerfile|tdb/*|tools/chat-feed/*) game=1 ;;
      monitoring/*|exporters/*|tools/wowmap/*|tools/dbc/*) mon=1 ;;
    esac
  done
  [ "$game" = 1 ] && echo game
  [ "$mon" = 1 ] && echo monitoring
  return 0
}
if [ "${1:-}" = "--stacks-for" ]; then stacks_for; exit 0; fi  # test hook

if [ ! -f .env ] && [ "${DRY_RUN:-0}" != 1 ]; then
  echo "error: $(pwd)/.env is missing — copy .env.example and fill in the secrets (see docs/DEPLOYMENT.md)" >&2
  exit 1
fi

git fetch -q origin
FROM=$(git rev-parse HEAD)
TO=$(git rev-parse origin/main)
if [ "${ALL:-0}" = 1 ]; then
  STACKS="game monitoring"
else
  STACKS=$(git diff --name-only "$FROM" "$TO" | stacks_for | tr '\n' ' ')
fi
STACKS=${STACKS% }
echo "== ${FROM:0:7} -> ${TO:0:7}, stacks: ${STACKS:-none} =="

case " $STACKS " in *" game "*)
  # Same DB the exporter reads (wow_players_online). A failed query is "unknown": defer.
  ONLINE=$(docker exec trinitycore-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM characters.characters WHERE online = 1"' 2>/dev/null) || ONLINE=unknown
  if [ "$ONLINE" != 0 ] && [ "${FORCE:-0}" != 1 ]; then
    echo "$(date -Is) DEFERRED: game stack deploy waits, players online: $ONLINE (FORCE=1 to override)"
    exit 0
  fi ;;
esac

if [ "${DRY_RUN:-0}" = 1 ]; then echo "dry run: nothing changed"; exit 0; fi

git reset --hard "$TO"

case " $STACKS " in *" game "*)
  echo "== game stack =="
  docker compose up -d --build ;;
esac
case " $STACKS " in *" monitoring "*)
  echo "== monitoring stack =="
  docker compose --env-file .env -f monitoring/docker-compose.yml up -d --build ;;
esac

M=${METRICS_DIR:-/opt/wow-server-metrics}
{ mkdir -p "$M" && echo "$(date -Is) deployed ${TO:0:7}, stacks: ${STACKS:-none}" >> "$M/deploy.log"; } || true

echo "== status =="
docker compose ps
docker compose --env-file .env -f monitoring/docker-compose.yml ps
