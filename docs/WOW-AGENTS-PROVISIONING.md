# Provisioning the `wow-agents` guest

Runbook for issue #135: a dedicated VM on `pv1` that runs the agent fleet so the
agents stop competing with the worldserver, MySQL, the chat feed and wowmap for the
6 GB `wow-server` VM. The decisions behind it (VM, not LXC; audit is **pulled** by
the wow-server VM; wowmap stays where it is) are ADR 0002 (D1, D3, D4).

> **Naming trap.** VM 305 is called `agents` but is the coding-CLI dev host (no
> Docker). The WoW fleet's guest is **`wow-agents`**. Nothing below touches VM 305.

Every command here is for a **human** to run. The repo half (this document, the
bootstrap script, the audit pull) is tested offline only; nothing in it has been run
against `pv1`, the guest or the live realm.

| Where | Runs | From |
|---|---|---|
| `pv1` (192.168.1.75) | `qm ...`: create the guest | section 1 |
| `wow-agents` guest | `scripts/wow-agents/bootstrap-guest.sh`, Docker, the agents | sections 2-4 |
| `wow-server` VM (192.168.1.64) | audit pull timer + units | section 5 |

Placeholders to decide first (nothing below picks them for you):

```bash
VMID=$(pvesh get /cluster/nextid)   # or pick one by hand; `qm list` shows what is taken
IP=192.168.1.NN                     # a free address in 192.168.1.0/24 (known in use: .60 .64 .72 .73 .75 .77)
GW=192.168.1.1                      # check: `ip route | grep default` on pv1
DNS=192.168.1.1                     # or the pihole (LXC 203)'s address
```

Check that the address is free (`ping -c2 $IP` fails, `arping -c2 $IP` stays silent)
and that the bridge is really `vmbr0` (`ip -br link` on pv1).

## 1. Create the VM (on `pv1`)

Spec: 2 vCPU, 4 GiB RAM, 40 GB on `local-lvm`, `cpu host`, start on boot, Ubuntu
24.04 LTS cloud image, static IP via cloud-init. The compose file's 128 MB caps add
up to ~3.2 GB of *limits* for 25 agents, not of usage: record the real numbers in
section 7 before resizing anything.

```bash
# An SSH public key for the `ubuntu` login (yours; never a private key)
cat > /root/wow-agents-admin.pub <<'KEY'
<paste your ssh-ed25519 public key here>
KEY

cd /var/lib/vz/template/iso
wget -O ubuntu-24.04-cloudimg-amd64.img \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

qm create $VMID --name wow-agents --ostype l26 \
  --cpu host --sockets 1 --cores 2 --memory 4096 --balloon 0 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single \
  --agent enabled=1 --onboot 1

qm importdisk $VMID /var/lib/vz/template/iso/ubuntu-24.04-cloudimg-amd64.img local-lvm
qm set $VMID --scsi0 local-lvm:vm-$VMID-disk-0,discard=on,iothread=1
qm resize $VMID scsi0 40G

qm set $VMID --ide2 local-lvm:cloudinit --boot order=scsi0 \
  --serial0 socket --vga serial0
qm set $VMID --ciuser ubuntu --sshkeys /root/wow-agents-admin.pub \
  --ipconfig0 ip=$IP/24,gw=$GW --nameserver $DNS

qm start $VMID
```

Verify from `pv1` and your workstation:

```bash
qm config $VMID | grep -E 'name|cores|memory|cpu|onboot|scsi0|ipconfig0'
ssh ubuntu@$IP 'hostname; nproc; free -m | head -2; df -h / | tail -1; timedatectl | grep "Time zone"'
```

Expect `wow-agents`, 2 CPUs, ~3.9 GB, ~38 GB root, `Time zone: Etc/UTC`. Keep the
zone UTC on both this guest and the wow-server VM: the pull status check compares
timestamps across them.

If storage or networking turns out wrong on the day and the guest must be an LXC
with Docker nesting, record the deviation in ADR 0002 rather than switching
silently.

## 2. Bootstrap the guest (on `wow-agents`)

```bash
ssh ubuntu@$IP
curl -fsSL https://raw.githubusercontent.com/um-dia-a-gente-faz/wow-server/main/scripts/wow-agents/bootstrap-guest.sh \
  -o /tmp/bootstrap-guest.sh
less /tmp/bootstrap-guest.sh          # read it before running it as root
sudo bash /tmp/bootstrap-guest.sh
```

Re-running is safe. It installs Docker Engine and the compose plugin, clones the repo
to `/opt/wow-server` (fast-forward only on a re-run, never `reset --hard`), creates
`/opt/wow-server-metrics/audit` (what `docker-compose.agents.yml` mounts at
`/data/audit` today) and `/opt/wow-agent-runtime/` (the ADR D5 runtime dir the future
runner will use), and creates the `audit-pull` user. It **never writes secrets and
never creates or overwrites `.env`**; it checks `.env` and prints what is missing.

> Audit path: today the compose file mounts `/opt/wow-server-metrics/audit` and the
> pull defaults to it. ADR 0002 names `/opt/wow-agent-runtime/audit` for the runner's
> generated compose file. When the runner lands, set `AUDIT_SRC_PATH` /
> the `rrsync -ro` directory accordingly (section 5), nothing else changes.

### `.env` on the guest (human)

Create `/opt/wow-server/.env`, mode 600, owner root, from the wow-server VM's `.env`
over a trusted channel or by hand. The agents need `AGENT_PASSWORD` (required),
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` and/or `JEV_BASE_URL`/`JEV_API_KEY`/`JEV_MODEL`
for the brain, optionally `AGENT_HTTP_PUBLISH_IP`. The guest does **not** need
`MYSQL_ROOT_PASSWORD` or `ACCESS_PASSWORD`; leave them out. Do not copy
`.env.example` verbatim (its `change-me` values would log in with a wrong password).

```bash
sudo install -m 600 -o root -g root /dev/null /opt/wow-server/.env   # only if it does not exist yet
sudoedit /opt/wow-server/.env
sudo bash /tmp/bootstrap-guest.sh | tail -12                       # re-check: reports MISSING/PLACEHOLDER keys by name
git -C /opt/wow-server check-ignore .env                           # prints ".env": it is gitignored
```

## 3. Build the image from current `main` (on `wow-agents`)

`docker compose up -d` does **not** rebuild an existing `wow-agent:latest`, so a
stale image keeps running after `git pull`. Build explicitly, every time `main`
moves:

```bash
cd /opt/wow-server
git pull --ff-only
docker build -t wow-agent:latest .
docker image inspect wow-agent:latest --format '{{.Created}} {{.Id}}'
git rev-parse --short HEAD     # note it: this is the commit the image was built from
# later, to roll the fleet onto a new build:
#   docker compose -f docker-compose.agents.yml up -d --build --force-recreate agent-luaprata
```

## 4. Smoke test: one agent, at least 10 minutes

**First make sure no agent is already logged in as that account elsewhere**: a second
login of the same account kicks the first. The old home is the wow-server VM; stop
only the agent containers there, never the game stack:

```bash
# on the wow-server VM (192.168.1.64): list, then stop only agent-* containers
docker ps --filter name=wow-agent- --format '{{.Names}} {{.Status}}'
docker compose -f /opt/wow-server/docker-compose.agents.yml --profile raid down
```

(`-f docker-compose.agents.yml` only: never `docker compose down` on the main file,
and never restart `trinitycore-wowserver`.)

**Seed the guest from the old audit data once**, before the first pull, so the pull
cannot replace a day's file in the mirror with a shorter new one (rsync copies
whichever side's file is newer):

```bash
# from the wow-server VM
sudo rsync -a --rsync-path='sudo rsync' /opt/wow-server-metrics/audit/ ubuntu@$IP:/opt/wow-server-metrics/audit/
```

Start one agent on the guest and watch it for 10 minutes:

```bash
cd /opt/wow-server
docker compose -f docker-compose.agents.yml up -d agent-luaprata   # AGENT01, port 9601
docker logs -f wow-agent-luaprata                                  # logged in? Ctrl-C when satisfied

# each of these, now and again after 10 minutes
curl -s  http://127.0.0.1:9601/healthz               # session attached, agent name
curl -s  http://127.0.0.1:9601/state | head -c 400   # stats, quests, inventory
ls -l --time-style=full-iso /opt/wow-server-metrics/audit/Luaprata/  # newest file mtime keeps moving
wc -l /opt/wow-server-metrics/audit/Luaprata/*.jsonl                 # line count keeps growing
curl -s http://$IP:9601/healthz                       # also from another LAN host (wow-server VM)
```

The audit directory is named after the agent (`<AGENT_NAME>/<day>.jsonl`). The
observability API is read-only and carries no secrets (see `agent/http_api.py`).

## 5. Audit pull (on the wow-server VM, ADR 0002 D4)

The agent host owns the audit dir; the wow-server VM pulls it every 30 s into
`/opt/wow-server-metrics/audit`, so wowmap's activity feed, the textfile metrics and
Grafana keep reading the path they always read. Read-only key, `rsync -a`, **never
`--delete`**, mirror pruned at 14 days on the pull side.

### 5a. Key pair (on the wow-server VM)

```bash
sudo install -d -m 700 /etc/wow-audit-pull
sudo ssh-keygen -t ed25519 -N '' -C wow-audit-pull -f /etc/wow-audit-pull/id_ed25519
sudo cat /etc/wow-audit-pull/id_ed25519.pub            # the PUBLIC half: copy this line
```

The empty passphrase is deliberate (unattended timer). The key is harmless on its
own: the guest pins it to `rrsync -ro /opt/wow-server-metrics/audit`, so it cannot
write, delete or run anything.

### 5b. Authorize it (on the guest)

```bash
sudo AUDIT_PULL_PUBKEY='ssh-ed25519 AAAA... wow-audit-pull' bash /tmp/bootstrap-guest.sh
sudo cat /home/audit-pull/.ssh/authorized_keys
# restrict,command="/usr/bin/rrsync -ro /opt/wow-server-metrics/audit" ssh-ed25519 AAAA... wow-audit-pull
```

### 5c. Pin the guest's host key and configure (on the wow-server VM)

```bash
sudo sh -c "ssh-keyscan -t ed25519 $IP > /etc/wow-audit-pull/known_hosts"
ssh-keygen -lf /etc/wow-audit-pull/known_hosts        # compare with the guest:
#   ssh ubuntu@$IP ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
sudo sh -c "printf 'AUDIT_SRC_HOST=$IP\n' > /etc/default/wow-audit-pull"
```

Other knobs (`AUDIT_SRC_PATH`, `AUDIT_DEST`, `AUDIT_RETENTION_DAYS`, ...) are listed at
the top of `scripts/wow-agents/pull-audit.sh`. If the key is not `rrsync`-restricted,
set `AUDIT_SRC_PATH=/opt/wow-server-metrics/audit/`.

### 5d. Install and start (on the wow-server VM)

The scripts arrive on the VM through the normal checkout (`/opt/wow-server`, updated
by auto-deploy after the PR merges: a merge is a deploy, so schedule it).

```bash
cd /opt/wow-server/scripts/wow-agents
sudo install -m 644 wow-audit-pull.service wow-audit-pull-failed.service wow-audit-pull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wow-audit-pull.service        # one manual run first
journalctl -u wow-audit-pull.service -n 20 --no-pager
cat /var/lib/wow-audit-pull/last-success           # "<epoch> <iso>"
sudo systemctl enable --now wow-audit-pull.timer
systemctl list-timers wow-audit-pull.timer
```

### 5e. Pull stalled, or just no agent activity?

A stalled pull must not look like quiet agents. Run on the wow-server VM:

```bash
set -a; . /etc/default/wow-audit-pull; set +a
sudo -E /opt/wow-server/scripts/wow-agents/audit-pull-status.sh; echo "exit=$?"
```

It prints three ages and a verdict:

| Verdict | Exit | Meaning | What to do |
|---|---|---|---|
| `OK` | 0 | pull healthy, mirror fresh | nothing |
| `QUIET` | 0 | pull healthy, source has nothing newer: the agents are silent | look at the agents (`docker ps`, logs, `/healthz` on the guest) |
| `PULL BEHIND` | 1 | the guest has newer files than the mirror | `journalctl -u wow-audit-pull.service`; run `pull-audit.sh` by hand |
| `PULL STALLED` | 2 | no successful pull for 120 s (or ever) | same; check SSH, the key, the host key, the guest being up |

By hand, the same comparison is: heartbeat age (`cat /var/lib/wow-audit-pull/last-success`)
vs newest mirror file (`find /opt/wow-server-metrics/audit -name '*.jsonl*' -printf '%T@ %p\n' | sort -n | tail -1`)
vs newest source file (`ssh ubuntu@$IP 'find /opt/wow-server-metrics/audit -name "*.jsonl*" -printf "%T@ %p\n" | sort -n | tail -1'`).
Fresh heartbeat + old mirror + old source = agents quiet. Old heartbeat, or source
newer than mirror = the pull.

Failure is also loud on its own: a non-zero pull fails the unit, `OnFailure` logs an
error line, and `journalctl -p err -t wow-audit-pull --since -1h` lists them. The pull
does not retry in a loop; the next 30 s tick is the retry. A failed pull never prunes
and never deletes anything.

## 6. Point wowmap at the guest

wowmap's "Agent mind" tab reads `AGENT_API_URLS` (`Name=http://host:port,...`, agent N
on port 9600+N). Once PR #152's `scripts/gen_agent_api_urls.py` is on `main`, generate
the value from the roster instead of typing it:

```bash
python3 scripts/gen_agent_api_urls.py --host $IP      # prints AGENT_API_URLS=Luaprata=http://$IP:9601,...
```

Put the printed line in `/opt/wow-server/.env` on the **wow-server VM**, then recreate
wowmap only (never the worldserver):

```bash
cd /opt/wow-server
docker compose --env-file .env -f monitoring/docker-compose.yml up -d wowmap
curl -s http://192.168.1.64:9400/api/agents | head -c 300
curl -s http://192.168.1.64:9400/api/character/Luaprata/activity | head -c 300   # events from the pulled audit
```

Until #152 is merged, write the line by hand for the agents that run:
`AGENT_API_URLS=Luaprata=http://$IP:9601`.

## 7. Record the footprint (`docker stats` template)

After the first few agents (then again at the full party/raid size) have run for at
least 15 minutes, on the guest:

```bash
date -Is; uptime; free -m
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}'
du -sh /opt/wow-server-metrics/audit
```

Paste the result into the issue (#135) using this template, so the next person knows
the real footprint:

```
wow-agents footprint, <date>, image built from <short sha>
agents running: <n> (<names>), up <h:mm>
guest: <used>/<total> MiB RAM (free -m), load <1m 5m 15m>, root disk <used>/<size>
per agent (docker stats): mem <min>-<max> MiB (cap 128), cpu <min>-<max>% (cap 25%)
audit growth: <MB per agent per hour>, <du -sh total>
verdict: <fits / needs more RAM / over-provisioned>; extrapolated to 25 agents: <MiB>
```

## Acceptance (issue #135): all human steps, none done by this PR

- [ ] Guest boots, Docker runs, checkout present, `.env` in place, gitignored (sections 1-2)
- [ ] `wow-agent:latest` built on the guest from current `main` (section 3)
- [ ] One agent runs at least 10 min on the guest with audit output and a working `/healthz` and `/state` (section 4)
- [ ] No agent containers start on the wow-server VM any more (section 4, `docker ps` there)
- [ ] Audit files show up on the wow-server VM within about a minute and `curl -s http://192.168.1.64:9400/api/character/<name>/activity` lists the agent's events (sections 5 and 6)
- [ ] Failure drill: stop the guest's sshd or block the key, see the unit fail, the error in the journal and `PULL STALLED` from the status script; restore and see it recover
- [ ] `docker stats` numbers recorded on the issue (section 7)
- [ ] `docs/DEPLOYMENT.md` updated (owned by the ADR issue #134; link this runbook from it)
- [ ] `AGENT_API_URLS` for wowmap updated (section 6)
