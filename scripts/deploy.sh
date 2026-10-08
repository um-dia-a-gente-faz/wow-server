#!/usr/bin/env bash
# Redeploy the wow-server VM (192.168.1.64) from the current state of `main`.
#
# Run directly on the VM (or via `ssh root@192.168.1.64 /opt/wow-server/scripts/deploy.sh`):
#
#   cd /opt/wow-server && ./scripts/deploy.sh
#
# Fast-forwards the checkout to origin/main, then runs `up -d --build` only for
# the stacks whose files changed. Both the game stack (docker-compose.yml) and
# the monitoring stack (monitoring/docker-compose.yml) live in this same
# checkout but run as separate compose projects.
#
# Path-aware: each stack is compared from the commit it was last deployed at
# (refs/deployed/<stack>) to origin/main, and only stacks whose files changed
# are touched (game: docker-compose.yml, tdb/, tools/chat-feed;
# monitoring: monitoring/, exporters/, tools/wowmap, tools/dbc). A docs, agent
# or test merge touches neither; the root Dockerfile is the agent image, in
# neither stack. Before the game stack, the script defers it
# while any character is online (agent characters included) or the count cannot
# be read: the game stack stays pending (refs/deployed/game does not move) and
# the other stacks still deploy. Exit status 75 means "game stack deferred".
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

# stacks_for: stdin = NUL-delimited changed paths, stdout = "game" and/or "monitoring".
stacks_for() {
  local f game=0 mon=0
  while IFS= read -r -d '' f; do
    case "$f" in
      docker-compose.yml|tdb/*|tools/chat-feed/*) game=1 ;;
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
HEAD_SHA=$(git rev-parse HEAD)
TO=$(git rev-parse origin/main)
# Last commit each stack was deployed at; unset means "as of HEAD".
since() { git rev-parse -q --verify "refs/deployed/$1" || echo "$HEAD_SHA"; }
# changed <stack>: prints <stack> if it has changes pending since its own ref, and nothing
# else (the game-ref range may also hold monitoring paths that were already deployed).
changed() {  # --no-renames: a rename out of tdb/ shows as a delete there; -z: no path quoting
  local s
  s=$(git diff --name-only --no-renames -z "$(since "$1")" "$TO" | stacks_for) || return
  grep -x "$1" <<<"$s" || true
}
if [ "${ALL:-0}" = 1 ]; then
  STACKS="game monitoring"
else
  # One plain assignment each: a failing `git diff` aborts here (set -e), before any update-ref.
  GAME_CHANGED=$(changed game)
  MON_CHANGED=$(changed monitoring)
  STACKS=$(echo $GAME_CHANGED $MON_CHANGED)
fi
echo "== ${HEAD_SHA:0:7} -> ${TO:0:7}, stacks: ${STACKS:-none} =="

M=${METRICS_DIR:-/opt/wow-server-metrics}
log() { { mkdir -p "$M" && echo "$(date -Is) $*" >> "$M/deploy.log"; } || true; }

DEFERRED=0
case " $STACKS " in *" game "*)
  # Same DB the exporter reads (wow_players_online). A failed query is "unknown": defer.
  ONLINE=$(docker exec trinitycore-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM characters.characters WHERE online = 1"' 2>/dev/null) || ONLINE=unknown
  if [ "$ONLINE" != 0 ] && [ "${FORCE:-0}" != 1 ]; then
    DEFERRED=1
    STACKS=${STACKS//game/}; STACKS=$(echo $STACKS)
    echo "$(date -Is) DEFERRED: game stack pending at ${TO:0:7}, players online: $ONLINE (FORCE=1 to override)"
    [ "${DRY_RUN:-0}" = 1 ] || log "DEFERRED game ${TO:0:7}, players online: $ONLINE"
  fi ;;
esac

if [ "${DRY_RUN:-0}" = 1 ]; then echo "dry run: nothing changed"; exit 0; fi

for s in game monitoring; do  # first run: both stacks count as deployed at the old HEAD
  git rev-parse -q --verify "refs/deployed/$s" >/dev/null || git update-ref "refs/deployed/$s" "$HEAD_SHA"
done
git reset --hard "$TO"

case " $STACKS " in *" game "*)
  echo "== game stack =="
  docker compose up -d --build ;;
esac
case " $STACKS " in *" monitoring "*)
  echo "== monitoring stack =="
  docker compose --env-file .env -f monitoring/docker-compose.yml up -d --build ;;
esac

git update-ref refs/deployed/monitoring "$TO"
[ "$DEFERRED" = 1 ] || git update-ref refs/deployed/game "$TO"
[ -n "$STACKS" ] && log "deployed ${TO:0:7}, stacks: $STACKS"

echo "== status =="
docker compose ps
docker compose --env-file .env -f monitoring/docker-compose.yml ps
[ "$DEFERRED" = 0 ] || exit 75
