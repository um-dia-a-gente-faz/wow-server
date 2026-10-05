#!/usr/bin/env bash
# Dump the irreplaceable MySQL schemas (auth, characters) to gzip files (#293).
# Runs mysqldump inside the DB container so the root password never leaves it.
#   BACKUP_DIR=/opt/wow-server-backups  KEEP_DAYS=14  DBS="auth characters"  DB_CONTAINER=trinitycore-db
# Add world (reproducible from the TDB) with DBS="auth characters world" if wanted.
set -euo pipefail
BACKUP_DIR=${BACKUP_DIR:-/opt/wow-server-backups}
KEEP_DAYS=${KEEP_DAYS:-14}
DBS=${DBS:-auth characters}
DB_CONTAINER=${DB_CONTAINER:-trinitycore-db}

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%dT%H%M%S)
for db in $DBS; do
  out="$BACKUP_DIR/$db-$stamp.sql.gz"
  docker exec "$DB_CONTAINER" sh -c \
    'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines "$1"' sh "$db" \
    | gzip > "$out.part"
  gzip -t "$out.part"
  mv "$out.part" "$out"
  echo "backup ok: $out ($(stat -c %s "$out") bytes)"
done
find "$BACKUP_DIR" -maxdepth 1 -name '*.sql.gz' -mtime "+$KEEP_DAYS" -delete
