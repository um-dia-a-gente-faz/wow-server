#!/bin/bash
# Wait until the TrinityCore worldserver + authserver actually RUN inside the
# container (not just docker-proxy binding the host ports), then report.
IP=192.168.1.64
DC="docker compose -f /opt/wow-server/docker-compose.yml"

remote() { ssh -o ConnectTimeout=8 root@$IP "$@" 2>/dev/null; }

for i in $(seq 1 180); do   # up to ~3h at 60s
  # container health
  st=$(remote "docker inspect -f '{{.State.Status}}' trinitycore-wowserver")
  case "$st" in
    restarting|exited|dead)
      echo "CONTAINER_BAD state=$st"
      remote "$DC logs --tail 40 trinitycore-wowserver" | sed 's/[^[:print:]]//g'
      exit 1;;
  esac

  ws=$(remote "docker exec trinitycore-wowserver pgrep -x worldserver")
  as=$(remote "docker exec trinitycore-wowserver pgrep -x authserver")
  mm=$(remote "docker exec trinitycore-wowserver pgrep -x mmaps_generator")

  if [ -n "$ws" ] && [ -n "$as" ]; then
    echo "SERVICES_UP after ~${i} min (worldserver pid=$ws, authserver pid=$as)"
    remote "$DC ps"
    echo "--- ports ---"
    remote "ss -tlnp | grep -E ':(8085|3724|3000)'"
    echo "--- log tail ---"
    remote "$DC logs --tail 30 trinitycore-wowserver" | sed 's/[^[:print:]]//g'
    echo "--- data volume ---"
    remote "docker exec trinitycore-wowserver du -sh /app/server/data/* 2>/dev/null"
    exit 0
  fi

  if [ $((i % 10)) -eq 0 ]; then
    echo "[${i} min] extraction running: mmaps=${mm:-done} ws=${ws:-no} as=${as:-no} $(remote "docker exec trinitycore-wowserver du -sh /app/server/data 2>/dev/null")"
  fi
  sleep 60
done
echo "TIMEOUT after 3h"
exit 2
