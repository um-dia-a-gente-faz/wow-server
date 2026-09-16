#!/usr/bin/env bash
# Auto-deploy: poll origin/main and redeploy the VM when it moves.
#
# Installed on the VM (192.168.1.64) via cron (docs/DEPLOYMENT.md):
#   */5 * * * * root /opt/wow-server/scripts/auto-deploy.sh >> /var/log/wow-auto-deploy.log 2>&1
#
# Polling instead of GitHub webhooks because GitHub cannot reach a LAN IP.
# Safe to run by hand anytime: no-op when the checkout already matches
# origin/main, and flock prevents overlapping runs (a build can take
# longer than the cron interval).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

exec 9>/tmp/wow-auto-deploy.lock
if ! flock -n 9; then
  echo "$(date -Is) another auto-deploy run in progress, skipping"
  exit 0
fi

git fetch origin -q
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "$(date -Is) up to date (${LOCAL:0:7})"
  exit 0
fi

echo "$(date -Is) deploying ${LOCAL:0:7} -> ${REMOTE:0:7}"
./scripts/deploy.sh
echo "$(date -Is) deploy done"
