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
#   0  QUIET              pull healthy; at least one agent has written no decision
#                         file for > QUIET_AFTER_S (source and mirror agree: this is
#                         agent silence). Names the quiet agents (#631).
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
# Freshness (QUIET vs OK) counts the decision day files only: the files directly
# in <agent>/ (live and rotated <day>.jsonl.N), not <agent>/lifecycle/ (#622).
mirror_day_newest="$(find "$dest" -mindepth 2 -maxdepth 2 -type f -name '*.jsonl*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)"
mirror_day_newest="${mirror_day_newest%.*}"
# PULL BEHIND compares the whole tree on both sides, lifecycle files included.
mirror_newest="$(find "$dest" -type f -name '*.jsonl*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)"
mirror_newest="${mirror_newest%.*}"
# Same base as the source line (whole tree); the decision-file age is reported next to it.
mirror_line="mirror:        "
if [ -n "$mirror_newest" ]; then
  mirror_line+="newest audit file $((now - mirror_newest)) s old"
else
  mirror_line+="no audit files"
fi
if [ -n "$mirror_day_newest" ]; then
  mirror_line+=", newest decision file $((now - mirror_day_newest)) s old"
else
  mirror_line+=", no decision files"
fi
echo "$mirror_line ($dest)"

# --- per agent (#631) ------------------------------------------------------
# An agent is each directory directly under $dest; its age is the newest decision day
# file directly in it (lifecycle/ does not count). Names are NUL-separated and sorted.
agent_names=()
while IFS= read -r -d '' name; do
  agent_names+=("$name")
done < <(find "$dest" -mindepth 1 -maxdepth 1 -type d -printf '%f\0' 2>/dev/null | LC_ALL=C sort -z)
quiet_names=()
for name in "${agent_names[@]}"; do
  agent_newest="$(find "$dest/$name" -mindepth 1 -maxdepth 1 -type f -name '*.jsonl*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)"
  agent_newest="${agent_newest%.*}"
  if [ -z "$agent_newest" ]; then
    echo "agent $name: no decision files"
    quiet_names+=("$name")
  else
    echo "agent $name: newest decision file $((now - agent_newest)) s old"
    if [ $((now - agent_newest)) -gt "$quiet_after" ]; then
      quiet_names+=("$name")
    fi
  fi
done

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

# No agent directory at all: today's text, unchanged.
if [ "${#agent_names[@]}" -eq 0 ]; then
  echo "VERDICT: QUIET - the pull is healthy and the source has nothing newer: no agent activity for over ${quiet_after}s."
  exit 0
fi

# Any quiet agent makes the verdict QUIET; OK needs every agent fresh.
if [ "${#quiet_names[@]}" -gt 0 ]; then
  printf -v quiet_list '%s, ' "${quiet_names[@]}"
  quiet_list="${quiet_list%, }"
  echo "VERDICT: QUIET - the pull is healthy and the source has nothing newer: no agent activity for over ${quiet_after}s from: ${quiet_list}."
  exit 0
fi

echo "VERDICT: OK - pull healthy, mirror fresh."
exit 0
