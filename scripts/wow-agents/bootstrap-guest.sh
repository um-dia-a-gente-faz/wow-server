#!/usr/bin/env bash
# Bootstrap the wow-agents guest (issue #135, ADR 0002 D1/D4/D5).
#
# Run ON THE NEW GUEST, as root, once, on a fresh Ubuntu LTS install:
#
#   curl -fsSL https://raw.githubusercontent.com/um-dia-a-gente-faz/wow-server/main/scripts/wow-agents/bootstrap-guest.sh | sudo bash
#   (or, from a checkout:  sudo scripts/wow-agents/bootstrap-guest.sh)
#
# What it does (idempotent, safe to re-run):
#   1. installs Docker Engine + the compose plugin from Docker's apt repo
#   2. clones the repo to /opt/wow-server (fast-forwards it on a re-run)
#   3. creates the audit dirs and the runtime dir:
#        /opt/wow-server-metrics/audit   what docker-compose.agents.yml mounts today
#        /opt/wow-agent-runtime/audit    the D5 runtime dir the future runner will use
#   4. creates the read-only `audit-pull` user the wow-server VM pulls with
#      (installs its authorized_keys line only if AUDIT_PULL_PUBKEY is given)
#   5. checks /opt/wow-server/.env and prints what a human must put in it
#
# What it never does: write a secret, create or overwrite `.env`, build the
# image, start an agent, or touch anything outside this guest.
#
# Optional environment:
#   REPO_URL           default https://github.com/um-dia-a-gente-faz/wow-server.git
#   REPO_BRANCH        default main
#   AUDIT_PULL_PUBKEY  the PUBLIC key (one line, "ssh-ed25519 AAAA... comment")
#                      generated on the wow-server VM; see the runbook
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/um-dia-a-gente-faz/wow-server.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
CHECKOUT=/opt/wow-server
AUDIT_DIR=/opt/wow-server-metrics/audit
RUNTIME_DIR=/opt/wow-agent-runtime
PULL_USER=audit-pull

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ -r /etc/os-release ] || die "no /etc/os-release; this script targets Ubuntu"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "expected Ubuntu, found '${ID:-unknown}'; install Docker by hand and adapt this script"

# --- 1. Docker ---------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
say "base packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git rsync openssh-server qemu-guest-agent

if docker compose version >/dev/null 2>&1; then
  say "docker + compose plugin already installed: $(docker --version)"
else
  say "installing Docker Engine and the compose plugin from download.docker.com"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker compose version >/dev/null || die "docker compose plugin missing after install"

# --- 2. Checkout -------------------------------------------------------------
if [ -d "$CHECKOUT/.git" ]; then
  say "checkout exists, fast-forwarding $REPO_BRANCH"
  git -C "$CHECKOUT" fetch --quiet origin "$REPO_BRANCH"
  git -C "$CHECKOUT" checkout --quiet "$REPO_BRANCH"
  git -C "$CHECKOUT" merge --ff-only --quiet "origin/$REPO_BRANCH" \
    || die "$CHECKOUT cannot fast-forward (local changes?); fix by hand, nothing was overwritten"
elif [ -e "$CHECKOUT" ]; then
  die "$CHECKOUT exists but is not a git checkout; move it away first"
else
  say "cloning $REPO_URL ($REPO_BRANCH) to $CHECKOUT"
  git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$CHECKOUT"
fi

# --- 3. Audit + runtime dirs -------------------------------------------------
say "creating $AUDIT_DIR and $RUNTIME_DIR"
# The agent containers run as root and write 0644 files; the pull user only reads.
install -d -m 0755 "$AUDIT_DIR" "$RUNTIME_DIR" "$RUNTIME_DIR/audit"

# --- 4. Read-only pull user --------------------------------------------------
if ! id "$PULL_USER" >/dev/null 2>&1; then
  say "creating system user $PULL_USER (password locked)"
  useradd --system --create-home --shell /bin/bash --comment "wow-server audit pull (read-only)" "$PULL_USER"
fi
install -d -m 0700 -o "$PULL_USER" -g "$PULL_USER" "/home/$PULL_USER/.ssh"

if [ -n "${AUDIT_PULL_PUBKEY:-}" ]; then
  case "$AUDIT_PULL_PUBKEY" in
    ssh-ed25519\ *) ;;
    *) die "AUDIT_PULL_PUBKEY must be one 'ssh-ed25519 AAAA...' line (the PUBLIC key)" ;;
  esac
  case "$AUDIT_PULL_PUBKEY" in *$'\n'*|*\"*|*\'*) die "AUDIT_PULL_PUBKEY must be a single line without quotes" ;; esac
  command -v rrsync >/dev/null || die "rrsync not found (it ships with the rsync package); cannot restrict the key"
  rrsync_bin="$(command -v rrsync)"
  # `restrict` drops pty, forwarding and agent; the forced command makes the key
  # read-only and confined to the audit dir (the remote path is then just "/").
  line="restrict,command=\"$rrsync_bin -ro $AUDIT_DIR\" $AUDIT_PULL_PUBKEY"
  auth="/home/$PULL_USER/.ssh/authorized_keys"
  touch "$auth"
  if grep -qxF "$line" "$auth"; then
    say "authorized_keys line for $PULL_USER already present"
  else
    printf '%s\n' "$line" >> "$auth"
    say "installed the read-only key for $PULL_USER"
  fi
  chown "$PULL_USER:$PULL_USER" "$auth"
  chmod 0600 "$auth"
else
  say "AUDIT_PULL_PUBKEY not given: no authorized_keys line installed (human step below)"
fi

# --- 5. .env: check only, never write ---------------------------------------
env_file="$CHECKOUT/.env"
env_ok=1
if [ -e "$env_file" ]; then
  say "$env_file exists: leaving it exactly as it is"
  chmod 600 "$env_file"
  git -C "$CHECKOUT" check-ignore -q .env || { echo "WARNING: .env is NOT gitignored here"; env_ok=0; }
  for key in AGENT_PASSWORD; do
    grep -Eq "^${key}=.+" "$env_file" || { echo "MISSING in .env: $key"; env_ok=0; }
    ! grep -Eq "^${key}=(change-me)?$" "$env_file" || { echo "PLACEHOLDER in .env: $key"; env_ok=0; }
  done
else
  env_ok=0
fi

cat <<EOF

Bootstrap done.  Docker: $(docker --version)
Checkout: $CHECKOUT @ $(git -C "$CHECKOUT" rev-parse --short HEAD)

HUMAN STEPS (this script writes no secrets):
EOF
if [ "$env_ok" -eq 0 ]; then
  cat <<EOF
  1. Create $env_file (mode 600, never committed). Copy it from the wow-server VM
     over a trusted channel, or write it by hand. It must contain at least:
       AGENT_PASSWORD=...      shared password of AGENT01..AGENT25 (required)
       LLM_BASE_URL / LLM_API_KEY / LLM_MODEL    if the agents use the LLM brain
       JEV_BASE_URL / JEV_API_KEY / JEV_MODEL    if the agents use Jev
     Optional: AGENT_HTTP_PUBLISH_IP (default all interfaces, i.e. LAN).
     Do NOT copy .env.example verbatim: its 'change-me' values would log in with
     the wrong password. Then:  chmod 600 $env_file
EOF
else
  echo "  1. .env present with AGENT_PASSWORD set. Nothing to do."
fi
if [ -z "${AUDIT_PULL_PUBKEY:-}" ]; then
  cat <<EOF
  2. Install the wow-server VM's PUBLIC pull key (see docs/WOW-AGENTS-PROVISIONING.md):
       re-run this script with AUDIT_PULL_PUBKEY='ssh-ed25519 AAAA... wow-audit-pull' set
EOF
fi
cat <<EOF
  3. Build the image from current main and smoke-test one agent (runbook, "Build and smoke test").
EOF
