#!/usr/bin/env bash
# Restore one backup made by backup-db.sh (#293).
#   restore-db.sh [--dry-run] <dir>/<db>-<stamp>.sql.gz
# The target is DB_CONTAINER (default: the live DB). Restoring over the live DB needs
# CONFIRM_LIVE=1; for a drill point DB_CONTAINER at a scratch MySQL (docs/DEPLOYMENT.md).
set -euo pipefail
DB_CONTAINER=${DB_CONTAINER:-trinitycore-db}
dry=0; [ "${1:-}" = "--dry-run" ] && { dry=1; shift; }
file=${1:?usage: restore-db.sh [--dry-run] <db>-<stamp>.sql.gz}
db=$(basename "$file"); db=${db%%-*}
[[ $db =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad database name '$db' in file name" >&2; exit 1; }

gzip -t "$file"
echo "restore $file -> database '$db' in container '$DB_CONTAINER'"
[ "$dry" = 1 ] && { echo "dry run: archive is valid, nothing restored"; exit 0; }
if [ "$DB_CONTAINER" = trinitycore-db ] && [ "${CONFIRM_LIVE:-}" != 1 ]; then
  echo "refusing to restore into the live DB without CONFIRM_LIVE=1" >&2; exit 1
fi
docker exec "$DB_CONTAINER" sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS \`$1\`"' sh "$db"
gunzip -c "$file" | docker exec -i "$DB_CONTAINER" sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$1"' sh "$db"
echo "restore ok"
