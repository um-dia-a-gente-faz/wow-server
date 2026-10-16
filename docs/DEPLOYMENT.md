# Deployment

TrinityCore 3.3.5a running on a dedicated Proxmox VM. This documents the **actual**
working deployment (not the original plan) and the failure modes that cost time.

For a fully automated from-scratch rebuild, use `docs/REPRODUCE-PROMPT.md` — a
self-contained prompt you can hand to an agent with SSH access to the Proxmox host.

## Current deployment

| | |
|---|---|
| Proxmox host | `pv1` @ 192.168.1.75 |
| VM | 100 `wow-server` @ **192.168.1.64** |
| Specs | 4 vCPU, 6 GB RAM, 50 GB disk, Ubuntu 24.04, Docker 29 |
| Stack | `/opt/wow-server`, a git checkout of this repo (`docker-compose.yml`) |
| Monitoring | `/opt/wow-server/monitoring/docker-compose.yml` (separate compose project) |

Ports: **8085** world · **3724** auth/logon · **3000** web UI · **3443** RA ·
**3306** MySQL · **9500** chat feed, plus the monitoring stack's :9100/:8080/:9300/:9400
(full table in `docs/ARCHITECTURE.md`). All LAN-only (`192.168.1.0/24`), never
exposed to the internet.

## Prerequisites

- Docker 29+ and Compose v5 on the VM
- ~20 GB free disk (client 17 GB + extracted maps ~3 GB + DB)
- **`--cpu host` on the Proxmox VM** — see below, this is not optional
- WoW 3.3.5a client (build 12340), extrators read `Data/*.MPQ`
- The TDB full world SQL matching the binary (`tdb/README.md`)
- The pinned worldserver image, TDB release and TrinityCore revision are recorded in `SERVER_VERSION` (checked against `docker-compose.yml` and `tdb/README.md` by `tools/tests/test_server_version.py`, #398)

## Deploy

```bash
ssh root@192.168.1.64
mkdir -p /opt/wow-server && cd /opt/wow-server

# 1. Get the repo (piping avoids scp approval prompts)
ssh-keyscan github.com >> ~/.ssh/known_hosts
git clone git@github.com:um-dia-a-gente-faz/wow-server.git .

# 2. Client files — 17 GB, BEFORE starting (map extraction needs them)
#    From a machine that has them:
#    cd "WoW 3.3.5a" && tar czf - . | ssh root@192.168.1.64 \
#      'mkdir -p /opt/wow-server/client && cd /opt/wow-server/client && tar xzf -'

# 3. TDB (see tdb/README.md) — extract into ./tdb/

# 4. Adjust PUBLIC_IP_ADDRESS in docker-compose.yml if the VM IP differs

# 5. Secrets — gitignored .env next to docker-compose.yml (see "Updating")
cp .env.example .env && chmod 600 .env && $EDITOR .env

# 6. Start
docker compose up -d
docker compose logs -f trinitycore-wowserver
```

First boot: creates schemas → applies TDB → extracts maps (**~30 min on 4 vCPU**;
mmaps alone is ~29 min) → starts authserver + worldserver.

### Expected success output

```
>> World database is up-to-date! Containing 76 new and 6853 archived updates.
Using World DB: TDB 335.25101
MMaps were built in 29 Minutes 29 Seconds
World initialized in 0 minutes 7 seconds
TrinityCore rev. ... (worldserver-daemon) ready...
TC>
```

## Debugging the bootstrap

**`docker logs` will NOT show the real error.** The image's `CommandExecuter` captures
worldserver's stdout into its own tracker, so all you see is the Node crash. To read
the actual message, run the binary by hand in a throwaway container:

```bash
docker run -d --name wow-debug --network wow-server_default \
  -v wow-server_server_data:/app/server/data \
  -v wow-server_server_logs:/app/server/logs \
  -v /opt/wow-server/client:/app/client \
  -e PUBLIC_IP_ADDRESS=192.168.1.64 \
  --entrypoint /bin/sh danielsilvestre37/trinitycore-docker:3.3.5 -c "sleep infinity"

docker exec wow-debug sh -c 'sed -e "s|<DATABASE_HOST>|database|g" \
  -e "s|<DATABASE_PORT>|3306|g" -e "s|<DATABASE_USER>|trinity|g" \
  -e "s|<DATABASE_PASSWORD>|<see .env: TRINITY_DB_PASSWORD>|g" \
  /app/backend/resources/worldserver.335.conf.dist > /app/server/etc/worldserver.conf'

docker compose start database && sleep 12
docker exec wow-debug sh -c 'cd /app/server/bin && ./worldserver -u 2>&1 | head -20'
docker rm -f wow-debug
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `trinitycore-db` loops with `Fatal glibc error: CPU does not support x86-64-v2` | Proxmox VM has the default `kvm64` CPU; MySQL 8.4.4 needs x86-64-v2. `qm set <id> --cpu host` then `qm reboot` (needs a full restart). |
| `Could not populate the World database` / container restart-looping at "Updating application database" | Wrong TDB version bind-mounted. See `tdb/README.md`. |
| Bootstrap skips the TDB download even though `world` is empty | Image bug: `containsData()` only checks the `auth` DB. Reset clean: `docker compose down -v` (verify with `docker volume ls \| grep wow`) and re-up. |
| Port 3724/8085 "open" but client can't connect | `docker-proxy` binds the host ports the moment the container starts — **a port probe is a false positive** while map extraction is still running. Check `docker exec trinitycore-wowserver pgrep -x worldserver`. |
| Container OOM-killed | Memory limits exceed the VM's RAM. This compose uses 5g (wowserver) + 1g (MySQL) for a 6 GB VM. |

## Accounts

Accounts are created **server-side**, never in-client.

With a TTY:

```bash
docker attach trinitycore-wowserver
# Enter for the TC> prompt
account create <username> <password>
account set gmlevel <username> 3 -1
# Ctrl-P Ctrl-Q to detach
```

Without a TTY (scripted / remote agent) — `scripts/wow_console.py` drives the web UI's
socket.io console:

```bash
python3 scripts/wow_console.py 'account create <username> <password>' \
                               'account set gmlevel <username> 3 -1'
```

Verify in the DB (usernames are stored UPPERCASE; `expansion` 2 = WotLK;
`account_access` uses **`SecurityLevel`**, not `gmlevel`):

```bash
# MYSQL_ROOT_PASSWORD is already in the DB container's env (from .env)
docker exec trinitycore-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT id,username,expansion FROM auth.account;"'
docker exec trinitycore-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT * FROM auth.account_access;"'
```

## Monitoring

Host + container metrics are exported to the existing Prometheus/Grafana on the
docker-stack VM. See `monitoring/docker-compose.yml` for the full recipe and
`exporters/README.md` for the custom game exporter.

Three scrape targets on this VM:

| Port | Exporter | Data |
|---|---|---|
| 9100 | node-exporter | host CPU / RAM / disk / net |
| 8080 | cadvisor | per-container resources |
| 9300 | wow-exporter (custom) | game metrics: players online, characters, activity, economy |

Prometheus jobs: `node_wow`, `cadvisor_wow`, `wow_game` (all labelled
`host="wow-server"`).

- Prometheus: http://192.168.1.60:9091
- Grafana: http://192.168.1.60:3001
  - **WoW — Jogadores & Atividade** (`wow-players`)
  - **WoW — Saúde do Realm** (`wow-realm-health`)
  - **Wow Server — Host & Containers** (`wow-server-host`)
  - **Jev — decision usage** (`jev-decision-usage`)

Dashboards are managed by dropping a raw dashboard JSON into
`/opt/pandora/grafana/dashboards/` on the docker-stack VM (file provisioning, ~30 s
reload). The Grafana admin password is **not** `admin:admin` — it was rotated after
first boot, and the write API rejects basic auth; the file path needs no auth at all.

Validate every panel expression against Prometheus (`/api/v1/query_range`) before
committing a dashboard — an empty result almost always means a wrong label matcher.

## Database backups

`auth` and `characters` hold the owner's and every agent character and are the only
irreplaceable state (`world` is rebuilt from the TDB). `scripts/backup-db.sh` runs
`mysqldump --single-transaction` inside `trinitycore-db` (the password stays in the
container) and writes `<db>-<timestamp>.sql.gz` to `/opt/wow-server-backups`, deleting
files older than `KEEP_DAYS` (14) only when every dump succeeded, so a failing backup never
ages out the last good dumps. Override with `BACKUP_DIR`, `KEEP_DAYS`, `DBS`.

Install once on the VM, as root (idempotent, overwrites the same file):

```bash
echo '30 4 * * * root /opt/wow-server/scripts/backup-db.sh >> /var/log/wow-backup.log 2>&1' \
  > /etc/cron.d/wow-backup
```

Check it: `ls -l /opt/wow-server-backups`. Off-VM copy (optional), from the Proxmox host:
`rsync -a root@192.168.1.64:/opt/wow-server-backups/ /var/backups/wow/`.

**Restore drill** (never restore over the live DB for a drill; do not restart the worldserver):

```bash
docker run -d --name wow-scratch --memory 512m -e MYSQL_ROOT_PASSWORD=scratch mysql:8.4
until docker exec wow-scratch mysqladmin ping -h127.0.0.1 -uroot -pscratch --silent; do sleep 2; done  # TCP: the init-time temp server is socket-only
f=$(ls -t /opt/wow-server-backups/characters-*.sql.gz | head -1)
scripts/restore-db.sh --dry-run "$f"                  # validates the archive only
DB_CONTAINER=wow-scratch scripts/restore-db.sh "$f"
for c in trinitycore-db wow-scratch; do               # row counts must match
  docker exec $c sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM characters.characters"'
done
docker rm -f wow-scratch
```

Same for `auth` / `auth.account`. A real restore into the live DB needs `CONFIRM_LIVE=1`
and the worldserver stopped by the owner. `scripts/tests/test_backup_restore.py` covers
the scripts with a fake `docker` (CI has no MySQL).

Backup age is monitored: `wow-exporter` reads `/opt/wow-server-backups` (read-only mount) and
exposes `wow_backup_count`, `wow_backup_newest_timestamp_seconds` and
`wow_backup_newest_size_bytes`; the realm-health dashboard has a "Backups" row. The
`BackupStale` rule in `monitoring/prometheus-agent-alerts.yml` fires when the newest dump is
older than 26 h or there is none. Nothing ships that file to Prometheus: copy it to VM201, keep
it in `rule_files` of `prometheus.yml`, and reload Prometheus, or the alert does not exist.
The file holds the fleet rules as well (reconnects, decisions, router errors, Jev, `wow-exporter`);
each rule's `runbook` annotation links to its section in [RUNBOOK-ALERTS.md](RUNBOOK-ALERTS.md),
which says what to check and how to tune it.

## Updating

`/opt/wow-server` on the VM is a real clone of this repo (not a copy) and both
the game stack (`docker-compose.yml`) and the monitoring stack
(`monitoring/docker-compose.yml`, including `wow-exporter`, `wowmap`, and their
`../tools` / `../exporters` build contexts) run straight out of it as two
separate compose projects. To ship changes from `main`:

```bash
ssh root@192.168.1.64 '/opt/wow-server/scripts/deploy.sh'
```

### Automatic deploys (cron poller)

The VM redeploys itself: a cron job runs `scripts/auto-deploy.sh` every 5
minutes, which fetches `origin/main` and calls `deploy.sh` when the remote
moved or while a deferred game-stack change is still pending (polling, because
GitHub webhooks cannot reach a LAN IP; flock prevents overlapping runs). Installed as `/etc/cron.d/wow-auto-deploy`:

```
*/5 * * * * root /opt/wow-server/scripts/auto-deploy.sh >> /var/log/wow-auto-deploy.log 2>&1
```

Merging a PR to `main` is therefore all it takes to ship — the VM picks it
up within 5 minutes. Watch `/var/log/wow-auto-deploy.log` on the VM for
history.

This fast-forwards the checkout to `origin/main` and runs `docker compose up -d
--build` only for the projects whose files changed (see "Path-aware deploys"
below), which in turn only recreates containers whose image, build context, or
compose file actually changed. Env-only changes to
`TC_WORLD__*` still require the `trinitycore-wowserver` container to be
recreated (not just restarted) for `ConfigurationWriter.js` to regenerate
`worldserver.conf` — `up -d` does this automatically when the compose file
changed.

**First run.** A checkout with no `refs/deployed/*` (a fresh VM) counts both stacks as
deployed at the `HEAD` it had before the run, then diffs from there. If `HEAD` is behind
`origin/main`, the stacks whose paths changed in between deploy on that first run (the game
stack subject to the players-online guard, like any deploy). If `HEAD` already equals
`origin/main`, nothing deploys: the poller prints "up to date" without calling `deploy.sh`, and
a `deploy.sh` run by hand plans no stack and sets both refs to `HEAD`. Either way a game-path
commit already at `HEAD` is never deployed by the poller; run `ALL=1 ./scripts/deploy.sh` once
by hand to bring both stacks up (see the table below; the guard applies to it too).

**Repairing a stuck ref (human step, VM operator only).** `refs/deployed/game` stays behind
`origin/main` while a game change is deferred, and a failed game build leaves it (and
`refs/deployed/monitoring`) in place, so every poll retries. **Retrying needs no command: leave
the ref alone.** The scripts never drop a pending change on their own. Move the ref only to
**skip** the pending game range (a deferred change that should not go out on its own, a build
that keeps failing), only **forward to `origin/main`**, and only by hand on the VM:

```bash
git -C /opt/wow-server rev-parse refs/deployed/game     # where the game stack was last deployed
git -C /opt/wow-server rev-parse origin/main            # where main was at the last poll
git -C /opt/wow-server update-ref refs/deployed/game origin/main   # skip: nothing pending for the game stack
```

With the ref at `origin/main` the poller prints "up to date" and brings nothing up. The skip is
not permanent: the skipped files are still in the checkout, so the next game-path merge applies
them together with its own change. Any other value is not a skip. `deploy.sh` compares the two
trees (`git diff <ref> origin/main`, not ancestry), so a ref set to a commit behind `origin/main`,
ahead of it or on another branch that differs from it in a game path plans the game stack again,
and `docker compose up -d --build` runs for it on the first poll with nobody online (the
players-online guard still applies). After a failed game build the monitoring changes of the same
merge are not deployed either, and the poller only looks at the game ref: after skipping, run
`./scripts/deploy.sh` once by hand, or they wait for the next merge.

Grafana dashboard JSONs (`monitoring/grafana-dashboard-*.json`) are provisioned
on the *separate* docker-stack VM (192.168.1.60), not this one:

```bash
./scripts/deploy-dashboards.sh   # run from a machine with SSH to 192.168.1.60
```

### Path-aware deploys and the players-online guard

`deploy.sh` compares, per stack, the commit that stack was last deployed at
(`refs/deployed/<stack>` in the VM's checkout) with `origin/main`, and only touches the stacks
whose files changed:

| Changed path | Stack redeployed |
|---|---|
| `docker-compose.yml`, `tdb/`, `tools/chat-feed/` | game |
| `monitoring/`, `exporters/`, `tools/wowmap/`, `tools/dbc/` | monitoring |
| `tools/common/` (copied into both images, ADR 0014) | game and monitoring |
| anything else (docs, `agent/`, `scripts/`, tests, the root `Dockerfile`, which is the agent image) | none, the checkout just advances |

Before the game stack, it counts online characters (`characters.characters WHERE online = 1`,
the same source as `wow_players_online`). If anyone is online, or the query fails, the game
stack is **deferred**: it logs `DEFERRED game <sha>` (also to `deploy.log`, once per pending commit: `refs/deployed/game-deferred` records the sha already logged) and the script exits
75. Other stacks still deploy; the game stack stays pending (each stack is diffed from
`refs/deployed/<stack>`, which a deferral does not move), and `auto-deploy.sh` keeps polling
until it goes through. Later merges therefore do not queue behind a deferral.
Each real deploy appends `<time> deployed <sha>, stacks: <list>` to
`/opt/wow-server-metrics/deploy.log` (`METRICS_DIR` overrides). `wow-exporter` reads that file, so deploys show as a `Deploys` annotation on the realm-health and agent-behaviour dashboards (#377). The poll does not push dashboards to Grafana: after a dashboard JSON changes, run `./scripts/deploy-dashboards.sh` by hand from a machine with SSH access to the docker-stack VM.

Two consequences to know (owner decision pending, see the PR for #266):

- **Agent characters count as players.** The headless agents are normally online, so
  without `FORCE=1` a game-stack change defers until they are all logged out.
- **A missing or broken `trinitycore-db` container also defers**, indefinitely, because
  the count cannot be read. Use `FORCE=1` to deploy the fix.

| Env | Effect |
|---|---|
| `FORCE=1` | deploy the game stack even with players online |
| `ALL=1` | deploy both stacks regardless of the diff (the old behaviour) |
| `DRY_RUN=1` | print the plan and the guard result, change nothing |

To check a merge left the worldserver alone, compare
`docker inspect -f '{{.State.StartedAt}}' trinitycore-wowserver` before and after.

### Required: `/opt/wow-server/.env`

Secrets are **not** tracked. The compose files interpolate them from a
gitignored `.env` in the checkout root, using `${VAR:?}` so a missing value
fails loudly instead of starting with an empty password. `deploy.sh` refuses
to run without the file, and `git reset --hard` leaves it alone (it's ignored).

| Variable | Used by | Notes |
|---|---|---|
| `MYSQL_ROOT_PASSWORD` | `database` (+ healthcheck), `wow-exporter`, `wowmap` | Only applied when `db_data` is first initialised. On the existing VM, set it to the password the DB **already** uses — changing it here does not change MySQL. |
| `ACCESS_PASSWORD` | `trinitycore-wowserver` web UI (:3000) | Username stays `admin`. |
| `AGENT_PASSWORD` | `docker-compose.agents.yml` | Shared password of AGENT01..AGENT25 (also read by `scripts/create_agent_roster.py`). |
| `WOW_AGENT_TAG` | `docker-compose.agents.yml` | Git SHA of the `wow-agent` image the agents run (#291). Printed by `scripts/build-agent-image.sh`. The agent-runner's image status follows it (#730): restart `agent-runner` after changing it, since the runner reads it from its environment. |
| `LLM_API_KEY` | agents (future LLM layer) | Router key from the FreeLLMAPI instance at `192.168.1.72:3001` (the old docker-stack one is gone); never paste it into docs. |

One-time setup on the VM:

```bash
cd /opt/wow-server
cp .env.example .env && chmod 600 .env
$EDITOR .env                      # fill in the real values

docker compose config -q && echo game ok
docker compose --env-file .env -f monitoring/docker-compose.yml config -q && echo monitoring ok
```

The monitoring stack is its own compose project rooted at `monitoring/`, so
compose won't auto-load the root `.env` for it — always pass `--env-file .env`
(as `deploy.sh` does) when running it by hand.

Avoid `$` in secret values (compose interpolates it), or single-quote the value
in `.env`.

Apart from `.env`, everything running is tracked in the compose files. If you
add a one-off env var or port directly on the VM, fold it back into the repo
(secrets via `.env` + `.env.example`, the rest with a comment explaining why)
instead of leaving it untracked, or the next `deploy.sh` will silently drop it.

## Agent roster (25 agents, profiles `party` and `raid`)

`agents/roster.json` lists every agent: account `AGENT01..AGENT25`, character,
race, class, gender (no secrets). AGENT01..05 are Luaprata, Farstrider,
Shadowblade, Sunspeaker and Spellweaver (their race/class are `null` until a real
run reads them back from the realm); AGENT06..25 are random Horde characters
(orc, undead, tauren, troll, blood elf; never Death Knights, which need an
existing level 55 character on the account). `docker-compose.agents.yml` is
**generated** from the roster, so never edit its services by hand.

**Where they run.** Not on the 6 GB `wow-server` VM, which has no room (see
*Resource limits*): the fleet lives on its own guest on `pv1`, a VM named
**`wow-agents`** (2 vCPU / 4 GiB / 40 GB, `cpu host`, Docker + compose) with a
checkout of this repo plus its own `.env` (`AGENT_PASSWORD`, `LLM_*`,
`AGENT_RUNNER_TOKEN`). Decision record: `docs/adr/0002-agent-host-topology.md`.
The compose file points `WOW_HOST` at `192.168.1.64`, so the agents only need
LAN access to it. Each container has `mem_limit: 128m` and `cpus: 0.25`, so 25
of them need about 3.2 GB of RAM (limit, not measured use) and 6 CPUs at the
cap; size the VM from `docker stats` once they run.

**Start stagger (#539).** The generator gives the agent at 0-based roster position `i`
`AGENT_START_DELAY_S = (3 * i) % 60`, so a fleet-wide `up -d` reaches the auth server over
about a minute instead of all at once (positions 0 to 19 get 0 to 57 s, 20 to 24 get 0 to 12 s
again). The constants are `START_DELAY_STEP_S` and `START_DELAY_PERIOD_S` in
`agents/compose.py`; there is no per-agent override. The delay is taken at every process start,
so a container restarted by `restart: unless-stopped` waits it again. The agent waits that
long once, before its first connection attempt, and logs it; the wait is not repeated on a
reconnect (those use the jittered backoff, #402), and a SIGTERM ends it at once. Retired
entries still count in the position, and agents the runner adds get the delay of their
roster position. While it waits, `GET /state` answers with the lifecycle state `connecting`
(no reason yet). Setting it is only for the compose file; the agent default is `0`, bounds
0 to 300. A closed or silent auth port needs no readiness probe: it is already the
`connect_failed` reason, retried with backoff and apart from `auth_rejected` (the server
answered and said no).

> **Do not confuse `wow-agents` with VM 305.** VM 305 is *called* `agents`, but
> it is the coding-CLI dev host (claude/codex/antigravity/opencode/kimi users,
> Ubuntu 26.04, no Docker). Nothing from the WoW fleet runs there.

**What runs where.**

| Thing | Host |
|---|---|
| Realm, MySQL, `chat-feed`, **`wowmap`** (the website, incl. the fleet panel), exporters | wow-server VM, 192.168.1.64 |
| Agent containers, `tools/agent-runner` (:9700), the audit directory's source copy | `wow-agents` VM |

`wowmap` still lives on the wow-server VM; it reaches the runner over the LAN.
The runner is the only thing that touches the Docker socket on `wow-agents`
(a host systemd unit, not a container) and writes runtime state, including its
own generated compose file and the audit dir, under `/opt/wow-agent-runtime/`,
never into the git checkout.

**Audit pull.** The agents write `<agent>/<day>.jsonl` to
`/opt/wow-agent-runtime/audit/` on `wow-agents`. A systemd timer on the
wow-server VM pulls it every ~30 s with `rsync -a` over SSH (read-only key,
never `--delete`) into `/opt/wow-server-metrics/audit/`, where wowmap's
activity feed, the node-exporter textfile metrics and Grafana read it
unchanged. If the pull fails, the game is unaffected: those views go stale and
catch up on the next successful pull (files are append-only). To tell
"agent silent" from "pull broken", compare the audit age at the source
(`GET /agents` on the runner) with the mirror's newest file. Details of the
timer and key are in the `wow-agents` provisioning ticket (#135).

**Create the accounts and characters** (needs `AGENT_PASSWORD` in the
environment and the realm and console reachable; the script uses the same
shared password as the compose file, it does not generate new ones):

```bash
python3 scripts/create_agent_roster.py --dry-run    # print the plan; no network, writes nothing
python3 scripts/create_agent_roster.py              # create what is missing; safe to re-run
```

A real run logs in as each account; if that fails it creates the account on the
worldserver console (output not echoed, it carries the password) and logs in
again; if the account has no character it creates the planned one, drawing a new
name if the server says it is taken. Accounts that already have a character are
left alone and their real race/class are written back to `agents/roster.json`.
A second run changes nothing.

**Change the roster, regenerate the compose file:**

```bash
python3 scripts/create_agent_roster.py --write-plan   # (re)plan missing entries into agents/roster.json, no network
python3 scripts/gen_agents_compose.py                 # rewrite docker-compose.agents.yml
python3 scripts/gen_agents_compose.py --check         # CI-style check that it is up to date
```

**Start agents** (on `wow-agents`, in the checkout):

```bash
docker compose -f docker-compose.agents.yml --profile party up -d   # AGENT01..05
docker compose -f docker-compose.agents.yml --profile raid  up -d   # all 25
docker compose -f docker-compose.agents.yml up -d agent-luaprata    # one agent, any profile
docker compose -f docker-compose.agents.yml --profile raid down     # stop them
```

Plain `up -d` with no profile and no service name starts nothing. The first 5
agents are in both profiles, so `--profile raid` includes the party. Agent N
publishes its read-only API on port `9600+N` (9601..9625).

### Fleet limits file (ADR 0017, #747)

Every agent service mounts one host directory read-only at `/data/fleet`. The DIRECTORY, never
the file: the writer replaces the file by rename, and a single-file bind mount would pin the old
inode. The host path is `/opt/wow-server-metrics/fleet` in the committed compose file and
`/opt/wow-agent-runtime/fleet` (`<runtime>/fleet`) in the file `tools/agent-runner` renders.
`AGENT_FLEET_LIMITS_FILE` is passed through from `.env` and is **empty by default**: empty is off,
the agents touch no file and behave as before. Recreating the agents with this change alone
changes nothing; the owner arms it.

**Arm it, in this order** (on `wow-agents`). `<fleet dir>` is `/opt/wow-agent-runtime/fleet` for
agents started through `tools/agent-runner` (its runtime directory, `AGENT_RUNNER_RUNTIME_DIR`,
default `/opt/wow-agent-runtime`; its audit directory is `/opt/wow-agent-runtime/audit`) and
`/opt/wow-server-metrics/fleet` for agents started from the committed compose file (audit
`/opt/wow-server-metrics/audit`). Give the writer the matching `FLEET_AUDIT_DIR` and
`FLEET_LIMITS_FILE=<fleet dir>/limits.json`.

0. Create the directory as the writer's user, before the first `docker compose up`:
   `sudo install -d -o <writer user> <fleet dir>` (the runtime directory is root-owned). Docker
   creates a missing bind-mount source as `root`: the container can read it (state `missing`,
   which blocks nothing) but the writer cannot write it. If Docker already created it:
   `sudo chown <writer user> <fleet dir>`.
1. Start `tools/fleet-limits` (see its README) with `FLEET_LIMITS_FILE=<fleet dir>/limits.json`.
2. Check the file appears and is rewritten every 15 s: `ls -l <fleet dir>/limits.json`.
3. Put `AGENT_FLEET_LIMITS_FILE=/data/fleet/limits.json` in `.env`.
4. Recreate the agents: `docker compose -f docker-compose.agents.yml up -d` (the same profile or
   service names you started them with). Agents recreated through `tools/agent-runner` take the
   variable from the runner's environment (its unit's `EnvironmentFile=/opt/wow-server/.env`),
   which overrides `.env` for the `docker compose` it runs, so after ANY change to the line run
   `systemctl restart agent-runner` first. (Reasoned from Compose's documented precedence, shell
   environment over `.env`, and from the runner's code; not run.)
5. `curl -s http://<agent-host>:9601/metrics | grep fleet_limits_state` on one agent: the line with
   `state="ok"` is `1`. `missing` means the agent cannot see the file (wrong path or directory);
   `invalid` means it cannot parse it; `off` means the variable did not reach the container.

**An owner stop by hand.** With the writer running, write `owner-stop.json` next to `limits.json`
(write a temporary file in that directory and `mv` it over):

```json
{"jev": {"until_ts": 1790000000, "reason": "maintenance window, back at 22:00"}}
```

`until_ts` is a unix time (`date -d '+2 minutes' +%s`); `jev` and `llm` are the brains. Within one
writer period (15 s) the writer merges it into `limits.json`, and each agent sees it on its next
think cycle, with no restart: the audit log then shows `jev_status: "fleet_blocked"`. The stop
lasts until `until_ts` and no longer, and it outlives a dead writer only through `limits.json`'s
own entry. **Lift it** by deleting `owner-stop.json` (or the entry). With no writer, put the same
entry under `"blocks"` in `limits.json`
(`{"version": 1, "written_ts": <now>, "blocks": {"jev": {"until_ts": ..., "reason": "..."}}}`)
and lift it by deleting `limits.json`.

**Disarm.** Remove the `AGENT_FLEET_LIMITS_FILE` line from `.env`, run
`systemctl restart agent-runner` (always, when disarming: the runner's environment would keep the
old value; same reasoning as step 4, not run) and recreate the agents. The state goes back to `off`;
check it with `curl -s http://<agent-host>:9601/metrics | grep fleet_limits_state`, the line with
`state="off"` is `1`. Removing only the file leaves the agents running with state `missing`,
which is also no block.

### Agent state directory (ADR 0021, #252)

Every agent service mounts one host directory read-write at `/data/state`:
`/opt/wow-server-metrics/agent-state` in the committed compose file, `<runtime>/state` (the
`agent-runner` runtime directory, `/opt/wow-agent-runtime/state` by default) in the file it renders.
Each agent writes only its own subdirectory, `/data/state/<slug>`, named by `AGENT_STATE_DIR`, which
compose sets per service. It holds that agent's `names.json` (about 1 MB), so a container recreate or
image deploy keeps the name cache warm. The directory is created by Docker when first mounted, as root,
and the agent runs as root, so there is no ownership step. Nothing to arm: `AGENT_STATE_DIR` in `.env`
does not reach the containers.

To check on `wow-agents`: recreate one agent, then `ls -l /opt/wow-server-metrics/agent-state/<slug>/names.json`
must exist and keep its mtime across a second recreate.

### World bundle (ADR 0016, #749)

(The Docker and Compose behaviour in this section is reasoned from the compose file and the
documented bind-mount rules; not run: this change was made on a machine without docker.)

Every agent service mounts one host directory read-only at `/data/world`. The DIRECTORY, never
the file: `tools/worldknowledge/build.py` writes a temporary file next to `--out` and renames it
over the bundle, and a single-file bind mount would pin the old inode. The host path is
`/opt/wow-server-metrics/world` in the committed compose file and `/opt/wow-agent-runtime/world`
(`<runtime>/world`) in the file `tools/agent-runner` renders. `AGENT_WORLD_BUNDLE` is passed through
from `.env` and is **empty by default**: empty is off, no reader is built and `get_spell_info`
answers from `SPELL_TABLE` as before. Recreating the agents with this change alone changes nothing;
the owner arms it.

The agent opens the bundle once, at its start, and keeps the connection (`agent/worldknowledge.py`).
A rebuilt bundle is therefore seen by an agent at its NEXT start (`docker restart <container>` or
recreating it), not while it runs.

**Arm it, in this order** (on `wow-agents`). `<world dir>` is `/opt/wow-agent-runtime/world` for
agents started through `tools/agent-runner` (its runtime directory, default
`/opt/wow-agent-runtime`) and `/opt/wow-server-metrics/world` for agents started from the committed
compose file.

0. Create the directory as the user who receives the copy in step 1, before the first `docker compose up`:
   `sudo install -d -o <copy user> <world dir>`. Docker creates a missing bind-mount source as
   `root`. The agents only read the bundle, so they still read it there, but the copy in step 1 cannot
   write `world.sqlite3.new` into it. If Docker already created it: `sudo chown <copy user> <world dir>`.
1. Build the bundle from the client DBCs. Host: the wow-server VM, in the checkout (`/opt/wow-server`),
   where the client DBCs are (`/opt/wowmap-data/dbc`). Build to a local path, then copy the file to
   `wow-agents`. The build reads `Spell.dbc`, `SpellCastTimes.dbc` and `SpellRange.dbc` of client build
   12340 from one directory and writes one file; the real command, from `python3 tools/worldknowledge/build.py --help`:
   `python3 tools/worldknowledge/build.py --dbc-dir /opt/wowmap-data/dbc --out /tmp/world.sqlite3`.
   `tools/wowmap/README.md` lists `Spell.dbc` in that directory. `SpellCastTimes.dbc` and
   `SpellRange.dbc` were not checked, so check all three first:
   `ls -l /opt/wowmap-data/dbc/{Spell,SpellCastTimes,SpellRange}.dbc`. A missing file makes the build
   print `build failed, <out> untouched: <reason>` (`tools/worldknowledge/build.py`, the `print` in `main`)
   and leave the bundle as it was; that file then comes from the same client extraction as the others.
   On success it prints `spell: <n> rows`. The file is mode 0644 (check with `ls -l`).
   Copy it to a temporary name in `<world dir>`, then rename it on `wow-agents`:
   `scp /tmp/world.sqlite3 <agent-host>:<world dir>/world.sqlite3.new`, then on `wow-agents`
   `mv <world dir>/world.sqlite3.new <world dir>/world.sqlite3`, then `ls -l <world dir>`.
   Not a copy straight onto `world.sqlite3`: an in-place copy is seen half-written by an agent that
   starts meanwhile, and rewrites the file under agents that have it open; the rename replaces the
   name in one step and an open reader keeps the old file.
   Build in place (`--out <world dir>/world.sqlite3`) only on a host that has both the DBCs and
   `<world dir>`; `build.py` renames its own temporary file over the bundle, so that needs no copy.
2. Put `AGENT_WORLD_BUNDLE=/data/world/world.sqlite3` in `/opt/wow-server/.env` (the path inside the
   container).
3. Agents recreated through `tools/agent-runner` take the variable from the runner's environment (its
   unit's `EnvironmentFile=/opt/wow-server/.env`), which overrides `.env` for the `docker compose` it
   runs, so after ANY change to the line run `systemctl restart agent-runner` first. (Reasoned from
   Compose's documented precedence, shell environment over `.env`, and from the runner's code; not run.)
4. Recreate ONE agent first, `docker compose -f docker-compose.agents.yml up -d agent-luaprata` (or
   through the runner). Check the mount and the setting in its container:
   `docker exec wow-agent-luaprata ls -l /data/world` and `docker exec wow-agent-luaprata printenv AGENT_WORLD_BUNDLE`.
5. Read its log for the lines of #742: `docker logs wow-agent-luaprata 2>&1 | grep "world bundle"`.
   The first is `world bundle has <n> of 17 SPELL_TABLE ids`. Then one line per spell whose numbers
   or name differ from the table, `world bundle differs for spell <id>: <field> table <x> bundle <y>`.
   A name line is only logged, never applied. `world bundle unavailable: <path>: <reason>` means the
   reader is off (a missing, corrupt or other-version file) and the table answers. Judge the
   differing lines before you recreate the rest of the fleet.

**Turn it off.** Remove the `AGENT_WORLD_BUNDLE` line from `.env`, run `systemctl restart agent-runner`
(always, when turning it off: the runner's environment would keep the old value; same reasoning as
step 3, not run) and recreate the agents. With the variable empty no reader is built. Removing only
the bundle file leaves the running agents on the bundle they opened; the next start logs
`world bundle unavailable` and answers from `SPELL_TABLE`, which is the same answer as off.

### Pinned images, agent image tag and rollback (#291)

Every `image:` and `FROM` is `name:<version>@sha256:<digest>`; CI fails on
`:latest`. Tool dependencies (`tools/wowmap`, `exporters/wow-exporter`
`requirements.txt`) are hash-locked, so pip refuses a changed file.

**Agent image.** `scripts/build-agent-image.sh` builds `wow-agent:<git-sha>`
(and `wow-agent:latest`, a convenience only). The compose file reads the tag
from `WOW_AGENT_TAG` in `.env`, the one setting.

The agent-runner never rebuilds an existing pinned tag on its own. It builds only a missing
`wow-agent:<WOW_AGENT_TAG>`, and only when the tag is valid (#730). #732: it also builds only when the
tag is the checkout's HEAD (`git rev-parse --short=12 HEAD`, the form the script tags with); otherwise
`start` answers HTTP 409 (`image_not_built`, or `tag_is_not_head` for `?build=1` on a present image) and
docker is not called, so a checkout can never be built under another checkout's tag. The stale flag in its status
(the image is older than the last change to `agent/` or the Dockerfile) is information only: it
means "build a new image and move the tag".

Rebuild and roll, in this order:

```bash
git -C /opt/wow-server pull --ff-only             # 1. the checkout at the commit to ship
scripts/build-agent-image.sh                      # 2. builds wow-agent:<sha>, prints the SHA
# 3. set WOW_AGENT_TAG=<sha> in /opt/wow-server/.env
sudo systemctl restart agent-runner               # 4. the runner and the compose it spawns read the tag at start
docker compose -f docker-compose.agents.yml --profile raid up -d   # 5. start or recreate the agents
# 6. check GET /agents on the runner: image.name is wow-agent:<sha>, present is true
#    (tools/agent-runner/README.md has the call)
```

Rollback, with the same order: set `WOW_AGENT_TAG` back to the previous SHA in `.env`, restart
`agent-runner`, then check `GET /agents` shows the image `present: true` before starting any agent (if
it is `false`, the old image was pruned: stop, check out that commit and run `scripts/build-agent-image.sh`).
Then start the agents and check `GET /agents` again. The runner does not rebuild a pinned tag on
its own, so a rollback to a SHA whose image is still on the host starts that image unchanged. If the
old image was pruned and you start anyway, `start` builds the checkout under that old tag, which
overwrites what the SHA names.

**Bumping pins.** Look up the new tag's multi-arch digest in the registry
(Docker Hub `.../tags/<tag>` `digest`, or `gcr.io/v2/<repo>/tags/list`), change
the tag and digest together, never the tag alone. For `requirements.txt`, take
the sha256 of every file listed on PyPI for the new version (`/pypi/<name>/<version>/json`),
including transitive dependencies. Merging a compose change recreates the affected
containers, so follow the usual deploy rules.

`requirements-dev.txt` (ruff, mypy, coverage; #737) is hash-locked the same way. CI installs it
with `pip install --require-hashes`. To bump a pin: read the new version's `info.requires_dist`
from `/pypi/<name>/<version>/json` (extras dropped, markers kept), pin each transitive dependency
at the newest release that satisfies it, and list every file's `urls[].digests.sha256` for each
pinned version (sdist and all wheels). Take the hashes from the index, never by hand. Update the
`RELEASE_FILES` table in `scripts/tests/test_requirements_locked.py` in the same change with the new
per-pin counts; a mismatch there is a stale table, not a bad hash. The CI lint and coverage jobs
prove the file: pip refuses an unlisted package or a wrong hash on Python 3.12.
A developer machine proves it the same way:
`python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.txt`.

**Changing a service's `image:` text recreates its container, even when the digest names the
same content.** Compose hashes the `image:` text, so adding or editing `@sha256:...` recreates
that service whatever the digest is. Comparing `docker image inspect <image> --format
'{{.RepoDigests}}'` on the VM with the pin only tells whether the image *content* changes
(a new TrinityCore build), not whether the containers restart.

**What `deploy.sh` does with a merge that changes these files.** It defers only the game stack
(`trinitycore-wowserver`, `trinitycore-db`, chat-feed) until 0 characters are online, and also
while the count cannot be read; `FORCE=1` overrides the wait. The monitoring stack
(node-exporter, cadvisor, wow-exporter, wowmap) is not deferred: when a merge touches its files
it is recreated on the next 5-minute run.

**Not done here.** Mirroring the TrinityCore image or building it from source is a
separate spike and ADR.

### wowmap's `AGENT_API_URLS`

The console's *Agent mind* tab needs `AGENT_API_URLS` in the wow-server VM's
`/opt/wow-server/.env` (`monitoring/docker-compose.yml` forwards it to wowmap).
Unset, `GET /api/agents` returns `{"agents": []}` and every agent view is a 404.
The value is derived, not hand-typed: names come from `agents/roster.json` in
roster order, ports are `9600+N` as in `docker-compose.agents.yml` (the script
imports them from `gen_agents_compose.py`, and a test compares the result with
the committed compose file). Only the **host** is yours to give, and it has no
default, because the agents no longer run on `192.168.1.64` (ADR 0002 / issue
#134 puts them on the `wow-agents` VM):

```bash
python3 scripts/gen_agent_api_urls.py --host <agent-host>    # or AGENT_HOST=<agent-host>
# AGENT_API_URLS=Luaprata=http://<agent-host>:9601,Farstrider=http://<agent-host>:9602,...
```

The script only prints. By hand, on the wow-server VM: put that line in `.env`
(replace an existing `AGENT_API_URLS=` line), then recreate wowmap only, which is
not a worldserver restart:

```bash
docker compose --env-file .env -f monitoring/docker-compose.yml up -d wowmap
```

Re-run it when the roster or the agent host changes. The output lists all 25
agents; trim it by hand if only some run (a listed agent that is down gives a
clean 502 for its views, not a broken page).
The agent ports must be reachable from the wow-server VM (they are published on
all interfaces unless `AGENT_HTTP_PUBLISH_IP` is set).

## Management

```bash
cd /opt/wow-server
docker compose ps                        # status
docker compose logs -f --tail 50         # logs
docker compose restart                   # restart
docker compose down                      # stop (keeps data)
docker compose down -v                   # WIPE — deletes DB, maps and logs
```

Full reset (re-extracts maps, ~30 min):

```bash
cd /opt/wow-server && docker compose down -v && docker volume ls | grep wow
rm -rf server_data/ server_logs/
docker compose up -d
```

## Resource limits

Set in `docker-compose.yml`, and they must fit the VM's **total** RAM:

| Service | Limit | Reservation |
|---|---|---|
| wowserver | 5 GB / 4 CPU | 3 GB / 2 CPU |
| MySQL | 1 GB | — |

## Staging realm

A second worldserver and database for protocol, deploy-script and schema changes (#400).
It runs on `wow-agents` on demand, never on the game VM (`192.168.1.64`): `scripts/staging.sh`
refuses to start where a container named `trinitycore-wowserver` exists. The stack is
`staging/docker-compose.yml` (project `wow-staging`, containers `staging-worldserver` and
`staging-db`). It shares no container name, host port, volume or network with the game stack, and
the same pinned images (`SERVER_VERSION`). `scripts/deploy.sh` never touches it.

Ports: auth **3725**, world **8086**, MySQL **127.0.0.1:3307** (loopback only). No chat-feed, no
web UI, no RA port published. An agent points at it with `WOW_HOST=<wow-agents IP>` and
`WOW_AUTH_PORT=3725`. The WoW client cannot reach it: its auth port is fixed at 3724.

First bring-up on `wow-agents`, from a clone of this repo outside `/opt/wow-server`:

```bash
cp .env.staging.example .env.staging   # gitignored; fill every value (dummy values in the example)
# STAGING_CLIENT_DIR: a copy of the WoW 3.3.5a client (17 GB, Data/*.MPQ), as in step 2 above
# STAGING_TDB_FILE: the TDB the binary expects, TDB_full_world_335.25101_2025_10_21.sql (tdb/README.md)
scripts/staging.sh config              # resolves the compose file; fails on a missing variable
scripts/staging.sh up                  # first boot bootstraps the database; follow it:
docker logs -f staging-worldserver
```

Then `scripts/staging.sh down` stops it (volumes stay), `scripts/staging.sh reset` wipes the database
and starts it again (`down -v`, then `up`), and `scripts/staging.sh config` only prints the file.
Restarting it is allowed: no player is on it.

The realm list advertises `auth.realmlist.port`, not the worldserver's port. That defaults to 8085,
and the image's bootstrap does not set it. `TC_WORLD__WorldServerPort=8086` in the compose file only
moves the worldserver's listen port. `scripts/staging.sh up` (and `reset`) therefore runs
`scripts/staging.sh realmport`, which sets `auth.realmlist.port` to 8086 for realm 1 and reads it back.
The row can be missing for a while after `up` (the image's bootstrap writes it), so `realmport`
retries up to `STAGING_REALMPORT_TRIES` (default 30) times, 10 s apart (`STAGING_REALMPORT_SLEEP`), and
exits 3 if it never reads 8086. It is idempotent and can be run again by hand. Check it after the first
`up`: `SELECT address, port FROM auth.realmlist` must show 8086.

### Staging accounts (seed)

No script seeds them: the image's console is interactive (`docker attach`, as for the game stack), and
the repo cannot verify a non-interactive path for `staging-worldserver`. By hand, with the password from
`STAGING_ACCOUNT_PASSWORD` in `.env.staging` (typed or pasted, never echoed into a log):

```bash
docker attach staging-worldserver     # at the TC> prompt; Ctrl-P Ctrl-Q detaches
account create STAGING01 <password>   # repeat for STAGING02 .. STAGING05
```

Then check: `docker exec staging-db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT username FROM auth.account WHERE username LIKE \"STAGING%\""'`.
Unverified on `wow-agents`: the staging compose sets no `stdin_open`/`tty`, so `docker attach` may show no
prompt. If it does not, this step is blocked and needs a change to `staging/docker-compose.yml`.

**Characters (one per account):** the WoW client cannot reach staging (auth port 3725, the client's is fixed
at 3724), and the console creates accounts only, so the characters come from the agent's own protocol. Run
this from a clone on `wow-agents`, with the same `.env.staging` as the smoke below:

```bash
set -a; . ./.env.staging; set +a
WOW_HOST="$STAGING_PUBLIC_IP" WOW_AUTH_PORT=3725 \
  python3 -m tools.agent_dev.staging_characters            # STAGING01..STAGING05; --accounts to narrow
```

It logs in to each account and lists its characters. An account with none gets one (`created Stagingone`,
an Orc Warrior); an account that has one is left alone (`kept <name>`). A `FAIL` line names the account and
the reason; the exit code is 1 when one fails. It refuses the live realm and never prints the password. The
smoke below needs this step done first.

### Smoke (login, enter world, logout, reconnect)

`python3 -m tools.agent_dev.staging_smoke` runs the agent's own login code against the staging auth port (3725) and
prints PASS or FAIL per step. It refuses the live realm (192.168.1.64:3724) and needs `WOW_HOST` and
`WOW_AUTH_PORT` set. Run it from a clone on `wow-agents`:

```bash
set -a; . ./.env.staging; set +a
WOW_HOST="$STAGING_PUBLIC_IP" WOW_AUTH_PORT=3725 WOW_ACCOUNT=STAGING01 \
  WOW_PASSWORD="$STAGING_ACCOUNT_PASSWORD" python3 -m tools.agent_dev.staging_smoke
```

Exit 0 when all six steps pass. Each account needs one character already (the smoke does not create one;
`tools.agent_dev.staging_characters` does, above). It is a manual check: not in `scripts/check.sh all`, not a CI job. Its
unit tests (`tools/tests/test_staging_smoke.py`) run against a stub login.

Not yet done (#400, follow-up): the chat-echo step and the resource measurement
(needs a live run on `wow-agents`).
