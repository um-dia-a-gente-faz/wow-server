#!/usr/bin/env bash
# One-command local dev stack (#389). Usage: dev/dev.sh up|down|logs|smoke
set -euo pipefail
cd "$(dirname "$0")"
dc() { docker compose -f docker-compose.yml "$@"; }

case "${1:-}" in
  up)    dc up -d --build; echo "wowmap http://127.0.0.1:9400  chat-feed :9500  agent API :9601" ;;
  down)  dc down -v ;;
  logs)  shift; dc logs -f "$@" ;;
  smoke) # boot, wait for an agent decision to reach wowmap's API, tear down
    trap 'dc down -v >/dev/null 2>&1' EXIT
    dc up -d --build
    for _ in $(seq 60); do
      if curl -fs http://127.0.0.1:9400/api/agent/Luaprata/brain | grep -Eq '"decisions": *\[ *\{'; then
        echo "smoke ok: decision visible in wowmap"; exit 0
      fi
      sleep 2
    done
    dc logs --tail 40; echo "smoke FAILED: no decision in wowmap after 120s" >&2; exit 1 ;;
  *) echo "usage: $0 up|down|logs|smoke" >&2; exit 2 ;;
esac
