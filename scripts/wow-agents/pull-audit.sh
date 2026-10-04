#!/usr/bin/env bash
# Pull the agents' decision-audit logs from the wow-agents guest into the
# directory the wow-server VM's consumers already read (ADR 0002 D4, issue #135).
#
# Runs ON THE WOW-SERVER VM (192.168.1.64) from wow-audit-pull.timer, every ~30 s.
# See docs/WOW-AGENTS-PROVISIONING.md for the one-time key and unit setup.
#
#   * rsync -a over SSH with a read-only key (the guest side forces
#     `rrsync -ro <audit dir>`, so the key cannot write or run anything else).
#   * NEVER --delete: an empty or rebuilt guest cannot wipe the history here.
#     (A test greps this file for the flag.)
#   * Mirror files older than AUDIT_RETENTION_DAYS are pruned on this side only.
#   * Success writes the heartbeat $STATE_DIR/last-success ("<epoch> <iso>").
#     Failure writes $STATE_DIR/last-failure, logs to stderr (the journal) and
#     exits non-zero, so the unit fails and OnFailure fires. No retry loop here:
#     the next timer tick is the retry.
#
# Config comes from the environment (the unit loads /etc/default/wow-audit-pull):
#   AUDIT_SRC_HOST            required, e.g. 192.168.1.77 (the wow-agents guest)
#   AUDIT_SRC_USER            default: audit-pull
#   AUDIT_SRC_PATH            default: /  (rrsync's root is the audit dir itself;
#                             use the real path if the key is not rrsync-restricted)
#   AUDIT_DEST                default: /opt/wow-server-metrics/audit
#   AUDIT_STATE_DIR           default: ${STATE_DIRECTORY:-/var/lib/wow-audit-pull}
#   AUDIT_SSH_KEY             default: /etc/wow-audit-pull/id_ed25519
#   AUDIT_SSH_KNOWN_HOSTS     default: /etc/wow-audit-pull/known_hosts
#   AUDIT_RETENTION_DAYS      default: 14 (match AGENT_AUDIT_RETENTION_DAYS)
#   AUDIT_RSYNC_TIMEOUT_S     default: 20 (I/O stall timeout passed to rsync)
#   RSYNC_BIN / SSH_BIN       default: rsync / ssh (tests put fakes here)
set -euo pipefail

src_host="${AUDIT_SRC_HOST:?set AUDIT_SRC_HOST in /etc/default/wow-audit-pull}"
src_user="${AUDIT_SRC_USER:-audit-pull}"
src_path="${AUDIT_SRC_PATH:-/}"
dest="${AUDIT_DEST:-/opt/wow-server-metrics/audit}"
state_dir="${AUDIT_STATE_DIR:-${STATE_DIRECTORY:-/var/lib/wow-audit-pull}}"
ssh_key="${AUDIT_SSH_KEY:-/etc/wow-audit-pull/id_ed25519}"
known_hosts="${AUDIT_SSH_KNOWN_HOSTS:-/etc/wow-audit-pull/known_hosts}"
retention_days="${AUDIT_RETENTION_DAYS:-14}"
rsync_timeout="${AUDIT_RSYNC_TIMEOUT_S:-20}"
rsync_bin="${RSYNC_BIN:-rsync}"
ssh_bin="${SSH_BIN:-ssh}"

log() { echo "wow-audit-pull: $*" >&2; }
stamp() { printf '%s %s\n' "$(date +%s)" "$(date -Is)"; }

# Write a state file atomically so a reader never sees half a line.
write_state() { # name, text
  printf '%s\n' "$2" > "$state_dir/.$1.tmp"
  mv -f "$state_dir/.$1.tmp" "$state_dir/$1"
}

case "$retention_days" in
  ''|*[!0-9]*) log "AUDIT_RETENTION_DAYS must be a number, got '$retention_days'"; exit 64 ;;
esac

mkdir -p "$state_dir" "$dest"

# Skip (successfully) when a previous pull is still running, e.g. a manual run.
exec 9>"$state_dir/lock"
if ! flock -n 9; then
  log "another pull is running, skipping"
  exit 0
fi

# StrictHostKeyChecking=yes: the host key is pinned in $known_hosts by a human.
ssh_cmd="$ssh_bin -i $ssh_key -o IdentitiesOnly=yes -o BatchMode=yes"
ssh_cmd+=" -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts"
ssh_cmd+=" -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3"

rc=0
# --no-owner/--no-group: as root rsync would otherwise try to chown to the
# guest's numeric uids. No --delete, ever.
"$rsync_bin" -a --no-owner --no-group --timeout="$rsync_timeout" \
  -e "$ssh_cmd" "$src_user@$src_host:${src_path%/}/" "$dest/" || rc=$?

case "$rc" in
  0) ;;
  24) log "some source files vanished mid-transfer (rotation); treating as success" ;;
  *)
    write_state last-failure "$(stamp) rsync_exit=$rc"
    log "FAILED: rsync exited $rc pulling $src_user@$src_host:$src_path into $dest; the mirror is going stale"
    exit "$rc"
    ;;
esac

# Prune the mirror only (never the source). Runs after the pull so that a
# failed pull leaves the history alone. Rotated files (.jsonl.N) match too.
find "$dest" -type f -name '*.jsonl*' -mtime "+$retention_days" -delete
find "$dest" -mindepth 1 -type d -empty -delete

write_state last-success "$(stamp)"
