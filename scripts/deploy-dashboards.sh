#!/usr/bin/env bash
# Push this repo's Grafana dashboard JSONs to the docker-stack VM (192.168.1.60),
# which file-provisions dashboards from /opt/pandora/grafana/dashboards-wow/
# (auto-reloads in ~30s, no restart needed). Run from a machine with SSH access
# to that VM — not from the wow-server VM itself.
#
#   ./scripts/deploy-dashboards.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST="${GRAFANA_HOST:-root@192.168.1.60}"
DEST="/opt/pandora/grafana/dashboards-wow/"

for f in monitoring/grafana-dashboard-*.json; do
  [ -e "$f" ] || continue
  name="$(basename "$f" | sed 's/^grafana-dashboard-//')"
  echo "== $f -> $HOST:$DEST$name =="
  python3 -m json.tool "$f" >/dev/null   # fail fast on invalid JSON
  scp "$f" "$HOST:$DEST$name"
done
