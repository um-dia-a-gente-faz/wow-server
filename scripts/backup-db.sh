#!/usr/bin/env bash
# Dump the irreplaceable MySQL schemas (auth, characters) to gzip files (#293).
# Runs mysqldump inside the DB container so the root password never leaves it.
#   BACKUP_DIR=/opt/wow-server-backups  KEEP_DAYS=14  DBS="auth characters"  DB_CONTAINER=trinitycore-db
# Add world (reproducible from the TDB) with DBS="auth characters world" if wanted.
# A failed dump does not skip the other schemas or the prune; the exit code is non-zero.
set -euo pipefail
umask 077  # dumps contain account password hashes
BACKUP_DIR=${BACKUP_DIR:-/opt/wow-server-backups}
KEEP_DAYS=${KEEP_DAYS:-14}
DBS=${DBS:-auth characters}
DB_CONTAINER=${DB_CONTAINER:-trinitycore-db}

mkdir -p "$BACKUP_DIR"
part=
trap '[ -z "$part" ] || rm -f "$part"' EXIT
stamp=$(date +%Y%m%dT%H%M%S)
rc=0
for db in $DBS; do
  out="$BACKUP_DIR/$db-$stamp.sql.gz"
  part="$out.part"
  if docker exec "$DB_CONTAINER" sh -c \
      'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines "$1"' sh "$db" \
      | gzip > "$part" && gzip -t "$part"; then
    mv "$part" "$out"
    echo "backup ok: $out ($(stat -c %s "$out") bytes)"
  else
    echo "backup FAILED: $db" >&2
    rm -f "$part"
    rc=1
  fi
  part=
done
find "$BACKUP_DIR" -maxdepth 1 \( -name '*.sql.gz' -o -name '*.sql.gz.part' \) -mtime "+$KEEP_DAYS" -delete
exit $rc
