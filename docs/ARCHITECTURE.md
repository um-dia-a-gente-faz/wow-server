# Architecture

## Overview

```
┌────────────────────────────────────────────────────────────────────────┐
│                  Proxmox host pv1 (192.168.1.75)                       │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  VM 100 — wow-server (192.168.1.64)                              │  │
│  │  Ubuntu 24.04, Docker 29, 4 vCPU / 6 GB / 50 GB                  │  │
│  │                                                                  │  │
│  │  /opt/wow-server/ = git checkout of this repo (scripts/deploy.sh)│  │
│  │                                                                  │  │
│  │  ┌─ docker-compose.yml ── project "wow-server" ───────────────┐  │  │
│  │  │ trinitycore-wowserver                                      │  │  │
│  │  │   :8085 world  :3724 auth  :3000 web UI  :3443 RA telnet   │  │  │
│  │  │   /app/client ← ./client/   /app/server/bin/TDB_*.sql      │  │  │
│  │  │   server_data, server_logs (volumes)                       │  │  │
│  │  │        │ depends_on (healthy)        │ server_logs (ro)    │  │  │
│  │  │        ▼                             ▼                     │  │  │
│  │  │ trinitycore-db  :3306        chat-feed  :9500              │  │  │
│  │  │   mysql:8.4.4, db_data         tails Server.log → SSE      │  │  │
│  │  └──────────────────────┬─────────────────────────────────────┘  │  │
│  │                         │ network wow-server_default             │  │
│  │  ┌─ monitoring/docker-compose.yml ── project "monitoring" ────┐  │  │
│  │  │ wow-exporter  :9300   game metrics  (reads trinitycore-db) │  │  │
│  │  │ wowmap        :9400   live map + API (reads trinitycore-db)│  │  │
│  │  │ node-exporter :9100   host metrics  (host network)         │  │  │
│  │  │ cadvisor      :8080   per-container metrics                │  │  │
│  │  └────────────────────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  VM 201 — docker-stack (192.168.1.60), not managed by this repo  │  │
│  │  Prometheus :9091  →  Grafana :3001  →  Caddy :80                │  │
│  │  scrapes 192.168.1.64:9100, :8080, :9300                         │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
          ▲                                   ▲
          │  LAN 192.168.1.0/24, no           │
          │  external exposure                │
  ┌───────┴────────┐              ┌───────────┴──────────────────┐
  │  WoW client    │              │  AI agents (agent/)          │
  │  3.3.5a 12340  │              │  docker-compose.agents.yml,  │
  │  realmlist →   │              │  one container per character │
  │  192.168.1.64  │              │  → :3724 auth, :8085 world   │
  └────────────────┘              └──────────────────────────────┘
```

Both compose projects run from the same checkout. `scripts/deploy.sh`
fast-forwards it to `origin/main` and runs `docker compose up -d --build` for each
(`docs/DEPLOYMENT.md` → "Updating"). The monitoring project joins the game
project's network (`wow-server_default`, declared `external`) so its services
can reach `trinitycore-db` by name.

## Components

### trinitycore-wowserver

Single container bundling:

- **authserver** — login authentication on port 3724
- **worldserver** — game world simulation on port 8085
- **Web UI** — browser management on port 3000 (also exposes the worldserver console
  over socket.io as `worldserver_input`; used by `scripts/wow_console.py`)
- **Bootstrap** — creates databases, downloads/imports the TDB, runs DB updates,
  configures the realm
- **Map extractors** — dbc / maps / vmaps / mmaps from the client directory
- **RA console** — worldserver's telnet remote-admin console on port 3443, enabled
  with `TC_WORLD__Ra.Enable=1` in `docker-compose.yml`

Image: `danielsilvestre37/trinitycore-docker:3.3.5`
Source: https://github.com/valcriss/trinitycore-docker

### trinitycore-db

MySQL 8.4.4 storing:

- `auth` — accounts, realm list, RBAC, logs
- `characters` — player characters, inventory, quests
- `world` — NPCs, items, spells, quests, game objects (populated from the TDB dump)

> MySQL 8.4.4 requires the x86-64-v2 CPU baseline. The Proxmox VM must be created with
> `--cpu host`; the default `kvm64` model lacks it and the container crash-loops.

Port 3306 is published on the VM for ad-hoc queries. `wow-exporter` and `wowmap`
connect over the compose network as `trinitycore-db:3306`.

### chat-feed

`tools/chat-feed`, built from the repo and part of the game compose project. Tails
TrinityCore's `Server.log` from the `server_logs` volume (mounted read-only),
normalises ChatLogScript lines and serves public chat as Server-Sent Events on
port 9500, with a bounded replay buffer. It's a prototype: see
`tools/chat-feed/README.md` and `docs/CHAT_FEED_SPIKE.md`.

### Monitoring stack (`monitoring/docker-compose.yml`)

A separate compose project, `monitoring`, so it survives `docker compose down -v` on
the game stack:

- **wow-exporter** (:9300): `exporters/wow-exporter`, game metrics from MySQL. See
  `exporters/README.md`.
- **wowmap** (:9400): `tools/wowmap`, live map page, JSON API (including
  character inspect) and calibration UI. It reads DBCs, map art and
  `calibration.json` from `/opt/wowmap-data` on the VM. See
  `tools/wowmap/README.md` and `docs/LIVE-MAP.md`.
- **node-exporter** (:9100, host network) and **cadvisor** (:8080) export host and
  per-container metrics.

Prometheus and Grafana run on the docker-stack VM (192.168.1.60) and aren't
defined in this repo. Dashboard JSONs in `monitoring/` are shipped there with
`scripts/deploy-dashboards.sh`.

### AI agents (`agent/`, `docker-compose.agents.yml`)

`agent/` is a pure-stdlib Python 3.3.5a client: SRP6 auth, world login, keepalive,
chat and target actions. The root `Dockerfile` packages it, and
`docker-compose.agents.yml` runs one container per agent character. The roster
is `agents/roster.json` (UM-63): 25 agents, AGENT01..AGENT05 being Luaprata,
Farstrider, Shadowblade, Sunspeaker and Spellweaver and AGENT06..AGENT25 random
Horde characters. The compose file is generated from it
(`scripts/gen_agents_compose.py`) and has profiles `party` (the first 5) and
`raid` (all 25). Agents connect out to :3724/:8085, publish only their read-only
observability API (9601..9625), and aren't started by `scripts/deploy.sh`; they
run on their own `pv1` guest, the **`wow-agents`** VM (not VM 305, which is the
coding-CLI dev host that happens to be called `agents`), see
`docs/DEPLOYMENT.md` ("Agent roster") and `docs/adr/0002-agent-host-topology.md`.

| Component | Host | Port | Role |
|---|---|---|---|
| Agent containers | `wow-agents` | 9601..9625 | one per character; read-only observability API |
| `tools/agent-runner` | `wow-agents` | 9700 | control plane: fleet status, start/stop, character creation; shared-token HTTP, LAN only, owns the Docker socket (#136) |
| `tools/wowmap` fleet panel | wow-server VM | 9400 | UI only; calls the runner over the LAN, never holds the Docker socket |
Perception (parsing update-object packets) is in progress. See `docs/ROADMAP.md`
and `docs/PROTOCOL-NOTES.md`.

### Volumes

| Volume | Purpose | Persists |
|---|---|---|
| `server_data` | Extracted maps (dbc, maps, vmaps, mmaps) | Yes — ~2.9 GB |
| `server_logs` | TrinityCore runtime logs (also read by `chat-feed`) | Yes |
| `db_data` | MySQL data files | Yes |
| `./client` | WoW 3.3.5a client (bind mount, read for extraction) | User-supplied, ~17 GB |
| `./tdb/*.sql` | TDB world dump (bind mount) | User-supplied, ~280 MB |
| `/app/tmp` | tmpfs for extraction temp files | Ephemeral |
| `/opt/wowmap-data/{dbc,maps,calibration.json}` | wowmap DBCs, map art, calibration (host bind mounts) | Yes, on the VM |

`/app/server/bin` is **not** a volume — anything placed there is lost when the
container is recreated. That is why the TDB file is bind-mounted rather than copied.

## Network

All traffic is LAN-only. No external exposure, no TLS. Every port published on
192.168.1.64:

| Port | Service | Compose file | Protocol |
|------|---------|--------------|----------|
| 3724 | Auth server (`trinitycore-wowserver`) | `docker-compose.yml` | TCP (WoW login protocol) |
| 8085 | World server (`trinitycore-wowserver`) | `docker-compose.yml` | TCP (WoW game protocol) |
| 3000 | Web UI (`trinitycore-wowserver`) | `docker-compose.yml` | HTTP (management + socket.io console) |
| 3443 | RA console (`trinitycore-wowserver`) | `docker-compose.yml` | Telnet (remote admin) |
| 3306 | MySQL (`trinitycore-db`) | `docker-compose.yml` | MySQL |
| 9500 | `chat-feed` | `docker-compose.yml` | HTTP (SSE chat stream, `/healthz`) |
| 9300 | `wow-exporter` | `monitoring/docker-compose.yml` | HTTP (Prometheus scrape) |
| 9400 | `wowmap` | `monitoring/docker-compose.yml` | HTTP (map page + JSON API) |
| 9100 | `node-exporter` (host network) | `monitoring/docker-compose.yml` | HTTP (Prometheus scrape) |
| 8080 | `cadvisor` | `monitoring/docker-compose.yml` | HTTP (Prometheus scrape) |

`docker-compose.agents.yml` publishes only the agents' read-only API, 9601..9625 (agent N on 9600+N). Those ports, and the agent runner on :9700, are on the `wow-agents` VM, not on 192.168.1.64.

## Bootstrap sequence

On first `docker compose up -d`:

1. MySQL starts and becomes healthy
2. Main container starts, waits for MySQL
3. Creates `auth`, `characters`, `world` and the `trinity` user if missing
4. Checks whether the DBs hold data — **only checks `auth`**, a known image bug
5. Downloads + extracts the TDB dump (may pick the wrong version — see `tdb/README.md`)
6. Writes authserver/worldserver configs from the bundled `.dist` templates
7. Runs `worldserver -u` — populates `world` from the TDB, applies updates
8. Configures the realm from `PUBLIC_IP_ADDRESS` / `REALM_NAME`
9. Extracts dbc → maps → vmaps → mmaps from `./client/` (**~30 min**)
10. Starts authserver + worldserver; web UI stays on :3000

On subsequent starts steps 3–9 are skipped; the servers start immediately.

> **Readiness caveat**: `docker-proxy` binds ports 3724/8085/3000 as soon as the
> container starts, so a TCP connect succeeds even while map extraction is running.
> Check `pgrep -x worldserver` inside the container instead.
