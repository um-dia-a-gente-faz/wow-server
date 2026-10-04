#!/usr/bin/env bash
# Tell "the audit pull has stalled" from "the agents are quiet" (issue #135, D4).
# Runs ON THE WOW-SERVER VM. Read-only; safe to run any time or from a check.
#
# Two independent signals:
#   1. Heartbeat: $STATE_DIR/last-success is rewritten by every successful pull
#      (pull-audit.sh). Older than STALE_AFTER_S  => the PULL is broken, whatever
#      the agents are doing. Needs no clock agreement with the guest.
#   2. Source vs mirror: the newest file mtime on the guest (listed read-only
#      over the same key with `rsync --list-only`) against the newest in the
#      mirror. Source newer than mirror by more than SLACK_S => the pull is
#      BEHIND even though it "succeeds". rsync prints mtimes in the source's
#      local time zone and `date -d` reads them in this host's, so this check
#      assumes both are in the same zone (the runbook sets UTC on both). The
#      heartbeat verdict does not depend on it.
#
# Verdicts (exit code):
#   0  OK                 pull healthy and the mirror is fresh
#   0  QUIET              pull healthy; no agent has written for > QUIET_AFTER_S
#                         (source and mirror agree: this is agent silence)
#   1  PULL BEHIND        pull ran, but the source has newer data than the mirror
#   2  PULL STALLED       no recent successful pull (or never)
#
# Env (same file as the unit, /etc/default/wow-audit-pull, if you `set -a; . it`):
#   AUDIT_DEST, AUDIT_STATE_DIR, AUDIT_SRC_HOST/USER/PATH, AUDIT_SSH_KEY,
#   AUDIT_SSH_KNOWN_HOSTS  (as in pull-audit.sh)
#   STALE_AFTER_S   default 120    heartbeat older than this = stalled (4 ticks)
#   QUIET_AFTER_S   default 300    mirror newest older than this = agents quiet
#   SLACK_S         default 120    allowed source-over-mirror lag
#   SKIP_SOURCE=1   skip the ssh check (heartbeat + mirror only)
#   RSYNC_BIN / SSH_BIN  (tests)
set -uo pipefail

dest="${AUDIT_DEST:-/opt/wow-server-metrics/audit}"
state_dir="${AUDIT_STATE_DIR:-/var/lib/wow-audit-pull}"
src_host="${AUDIT_SRC_HOST:-}"
src_user="${AUDIT_SRC_USER:-audit-pull}"
src_path="${AUDIT_SRC_PATH:-/}"
ssh_key="${AUDIT_SSH_KEY:-/etc/wow-audit-pull/id_ed25519}"
known_hosts="${AUDIT_SSH_KNOWN_HOSTS:-/etc/wow-audit-pull/known_hosts}"
stale_after="${STALE_AFTER_S:-120}"
quiet_after="${QUIET_AFTER_S:-300}"
slack="${SLACK_S:-120}"
rsync_bin="${RSYNC_BIN:-rsync}"
ssh_bin="${SSH_BIN:-ssh}"
now="${NOW_EPOCH:-$(date +%s)}"

# --- heartbeat -------------------------------------------------------------
hb=""
if [ -r "$state_dir/last-success" ]; then
  read -r hb _ < "$state_dir/last-success" || hb=""
fi
case "$hb" in ''|*[!0-9]*) hb="" ;; esac

if [ -n "$hb" ]; then
  echo "heartbeat:     last successful pull $((now - hb)) s ago"
else
  echo "heartbeat:     none (the pull has never succeeded here)"
fi
if [ -r "$state_dir/last-failure" ]; then
  echo "last failure:  $(cat "$state_dir/last-failure")"
fi

# --- mirror newest ---------------------------------------------------------
mirror_newest="$(find "$dest" -type f -name '*.jsonl*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)"
mirror_newest="${mirror_newest%.*}"
if [ -n "$mirror_newest" ]; then
  echo "mirror:        newest audit file $((now - mirror_newest)) s old ($dest)"
else
  echo "mirror:        no audit files in $dest"
fi

# --- source newest (optional) ---------------------------------------------
src_newest=""
if [ -n "$src_host" ] && [ "${SKIP_SOURCE:-0}" != "1" ]; then
  ssh_cmd="$ssh_bin -i $ssh_key -o IdentitiesOnly=yes -o BatchMode=yes"
  ssh_cmd+=" -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts -o ConnectTimeout=10"
  listing="$("$rsync_bin" -r --list-only --timeout=20 -e "$ssh_cmd" \
    "$src_user@$src_host:${src_path%/}/" 2>/dev/null)" || listing=""
  # "-rw-r--r--  size YYYY/MM/DD HH:MM:SS name": regular files only, newest first.
  newest_line="$(printf '%s\n' "$listing" | awk '/^-/ && $0 ~ /\.jsonl/ {print $3" "$4}' | LC_ALL=C sort | tail -1)"
  if [ -n "$newest_line" ]; then
    src_newest="$(date -d "${newest_line//\//-}" +%s 2>/dev/null)" || src_newest=""
  fi
  if [ -n "$src_newest" ]; then
    echo "source:        newest audit file $((now - src_newest)) s old ($src_user@$src_host)"
  else
    echo "source:        unavailable or empty (could not list $src_user@$src_host)"
  fi
fi

# --- verdict ---------------------------------------------------------------
if [ -z "$hb" ] || [ $((now - hb)) -gt "$stale_after" ]; then
  echo "VERDICT: PULL STALLED - no successful pull in the last ${stale_after}s; the mirror is stale regardless of agent activity. journalctl -u wow-audit-pull.service"
  exit 2
fi

if [ -n "$src_newest" ] && { [ -z "$mirror_newest" ] || [ $((src_newest - mirror_newest)) -gt "$slack" ]; }; then
  echo "VERDICT: PULL BEHIND - the source has newer audit data than the mirror (pull 'succeeds' but is not catching up). journalctl -u wow-audit-pull.service"
  exit 1
fi

if [ -z "$mirror_newest" ] || [ $((now - mirror_newest)) -gt "$quiet_after" ]; then
  echo "VERDICT: QUIET - the pull is healthy and the source has nothing newer: no agent activity for over ${quiet_after}s."
  exit 0
fi

echo "VERDICT: OK - pull healthy, mirror fresh."
exit 0
