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
minutes, which fetches `origin/main` and calls `deploy.sh` only when the
remote moved (polling, because GitHub webhooks cannot reach a LAN IP; flock
prevents overlapping runs). Installed as `/etc/cron.d/wow-auto-deploy`:

```
*/5 * * * * root /opt/wow-server/scripts/auto-deploy.sh >> /var/log/wow-auto-deploy.log 2>&1
```

Merging a PR to `main` is therefore all it takes to ship — the VM picks it
up within 5 minutes. Watch `/var/log/wow-auto-deploy.log` on the VM for
history.

This fast-forwards the checkout to `origin/main` and runs `docker compose up -d
--build` for both projects, which only recreates containers whose image, build
context, or compose file actually changed. Env-only changes to
`TC_WORLD__*` still require the `trinitycore-wowserver` container to be
recreated (not just restarted) for `ConfigurationWriter.js` to regenerate
`worldserver.conf` — `up -d` does this automatically when the compose file
changed.

Grafana dashboard JSONs (`monitoring/grafana-dashboard-*.json`) are provisioned
on the *separate* docker-stack VM (192.168.1.60), not this one:

```bash
./scripts/deploy-dashboards.sh   # run from a machine with SSH to 192.168.1.60
```

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
