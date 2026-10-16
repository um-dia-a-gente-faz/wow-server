#!/usr/bin/env bash
# Staging realm (#400): a second worldserver and database, for protocol, deploy-script and schema
# changes. Runs on wow-agents on demand, never on the game VM. Config: .env.staging (template
# .env.staging.example). Compose file: staging/docker-compose.yml.
#   scripts/staging.sh up | down | reset | config | realmport
#   up         start the staging stack (images are pinned in the compose file, nothing is built),
#              then realmport
#   down       stop it; volumes (the database) stay
#   reset      down -v, then up (and realmport): a fresh database
#   config     print the resolved compose file; works without .env.staging (uses the example)
#   realmport  set auth.realmlist port to 8086 (the port clients are told). Idempotent.
# Refuses (exit 2) when a container named trinitycore-wowserver exists here, i.e. this is the
# game VM, and, except for config, when .env.staging is missing. Not called by scripts/deploy.sh;
# never run in CI.
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-}"
case "$cmd" in
  up|down|reset|config|realmport) ;;
  *) echo "usage: $0 up|down|reset|config|realmport" >&2; exit 2 ;;
esac

# Listed first, into a variable: a pipe into grep -q can exit early and fail under pipefail.
names=$(docker ps -a --format '{{.Names}}') || { echo "error: cannot list containers (is docker running?)" >&2; exit 2; }
if grep -qx trinitycore-wowserver <<<"$names"; then
  echo "refusing: a container named trinitycore-wowserver exists on this host, so this is the game VM." >&2
  echo "staging never runs there (#400)." >&2
  exit 2
fi

envfile=.env.staging
if [ ! -f "$envfile" ]; then
  if [ "$cmd" != config ]; then
    echo "error: $(pwd)/.env.staging is missing: cp .env.staging.example .env.staging and fill it in" >&2
    exit 2
  fi
  envfile=.env.staging.example
fi

# --env-file replaces the default .env, so the game stack's secrets are never read here.
dc() { docker compose --project-directory . --env-file "$envfile" -f staging/docker-compose.yml "$@"; }

# The realm list advertises auth.realmlist.port, which defaults to 8085 and which the image's
# bootstrap does not set. WorldServerPort only moves the worldserver's listen port, so the row is
# set here. The password is expanded inside the container: it never reaches this script's output.
realmport() {
  docker exec staging-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "UPDATE auth.realmlist SET port=8086 WHERE id=1"'
}

case "$cmd" in
  up)        dc up -d; realmport ;;
  down)      dc down ;;
  reset)     dc down -v; dc up -d; realmport ;;
  config)    dc config ;;
  realmport) realmport ;;
esac
