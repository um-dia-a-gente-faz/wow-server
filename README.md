# World of Warcraft — Wrath of the Lich King (3.3.5a) Private Server

[![CI](https://github.com/Cividati/wow-server/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Cividati/wow-server/actions/workflows/ci.yml)

TrinityCore-based WoW server, Dockerized, running on a dedicated Proxmox VM.

**Status: live** at `192.168.1.64` (realm **Pandora**, build 12340), LAN-only.

## Quick start

```bash
# On the wow-server VM (192.168.1.64)
ssh root@192.168.1.64
mkdir -p /opt/wow-server && cd /opt/wow-server
git clone git@github.com:Cividati/wow-server.git .

# 1. Place your WoW 3.3.5a client in ./client/  (Data/*.MPQ, ~17 GB)
# 2. Fetch the TDB world dump into ./tdb/        (see tdb/README.md)
# 3. Start
docker compose up -d

# 4. Watch bootstrap in the web UI (or the logs)
#    http://192.168.1.64:3000
```

| Port | Service |
|------|---------|
| 8085 | World server |
| 3724 | Auth server (logon) |
| 3000 | Web UI |
| 3443 | Remote-admin (RA) telnet console |
| 3306 | MySQL (`trinitycore-db`) |
| 9500 | Chat feed SSE (`chat-feed`) |

The monitoring stack (`monitoring/docker-compose.yml`) adds :9100, :8080, :9300
and :9400 (see [Monitoring](#monitoring)).

First boot applies the TDB and extracts maps from the client — **~30 minutes on
4 vCPU** (mmaps alone ~29 min). The web UI shows live progress.

## Connecting a client

Edit `Data/<locale>/realmlist.wtf` in a WoW 3.3.5a (**build 12340**) client:

```
set realmlist 192.168.1.64
set patchlist 192.168.1.64
```

Launch **`Wow.exe` directly** — not the launcher (it tries to patch and breaks
TrinityCore compatibility).

Create an account server-side:

```bash
docker attach trinitycore-wowserver
# Enter for the TC> prompt
account create <username> <password>
account set gmlevel <username> 3 -1
# Ctrl-P Ctrl-Q to detach
```

Or, without a TTY: `python3 scripts/wow_console.py 'account create <user> <pass>'`
(see `scripts/`).

## Two bugs in the upstream image (both fixed here)

The `danielsilvestre37/trinitycore-docker:3.3.5` image is usable but its bootstrap has
defects that make a naive `docker compose up` fail in a restart loop:

1. **`Database.containsData()` only checks the `auth` database.** If `auth` has tables
   while `world` is empty, it skips the TDB download and `worldserver -u` then fails
   to populate an empty world DB.
2. **It downloads the *newest* TDB release**, but the bundled `worldserver` binary is
   compiled expecting **one specific filename**. Mismatch ⇒ `Could not populate the
   World database` ⇒ exit 1 ⇒ container restart loop.

Both are worked around by bind-mounting the exact TDB `.sql` into `/app/server/bin`
(see `docker-compose.yml` and `tdb/README.md`).

Also worth knowing: the bootstrap swallows worldserver's stdout, so `docker logs`
never shows the real error — see `docs/DEPLOYMENT.md` → "Debugging the bootstrap".

## Documentation

| Doc | Contents |
|---|---|
| `docs/REPRODUCE-PROMPT.md` | Self-contained prompt to rebuild the whole environment from scratch on Proxmox (agent-ready) |
| `docs/DEPLOYMENT.md` | Actual deployment, redeploying via `scripts/deploy.sh`, gotchas, debugging, troubleshooting table |
| `docs/CLIENT-SETUP.md` | Client configuration and troubleshooting |
| `docs/ARCHITECTURE.md` | Components, compose projects, volumes and the full port table |
| `docs/GM-COMMANDS.md` | Useful in-game GM commands |
| `docs/LIVE-MAP.md` | Live map: how it was built, DBC field order, extraction, alignment notes |
| `docs/ROADMAP.md` | What shipped, the agent perception dev plan, the operator dashboard plan |
| `docs/AI-AGENT-SPEC.md` | Spec for autonomous AI agents playing on the server |
| `CLAUDE.md` / `AGENTS.md` | Entry point for coding agents: hard rules, where things are, what to read. Skills live in `.claude/skills/` |
| `docs/AGENT-DIRECTION.md` | Owner decisions for the agents (autonomy, free models, in-game-only chat, milestones); overrides the spec where they differ |
| `docs/NEXT-AGENT-HANDOFF.md` | Handoff for the `agent/` protocol client: what works, how to run it |
| `docs/PROTOCOL-NOTES.md` | 3.3.5a wire-format notes (update-object layout, field indices), each checked against TrinityCore source |
| `docs/CHAT_FEED_SPIKE.md` | Why the chat feed tails `Server.log` instead of polling the DB |
| `exporters/README.md` | Custom game metrics exporter: catalog, build, and its gotchas |
| `grafana/` | Dashboard provisioning config — the file-based loading that replaced API auth |
| `tdb/README.md` | Which TDB version to use and why |
| `SESSION.md` | Hermes session id for this build-out, and what it covered |
| `tools/wowmap/README.md` | Live map service (:9400): map page, character inspect API, calibration |
| `tools/chat-feed/README.md` | Chat feed SSE sidecar (:9500): API, config, prototype limitations |
| `CONTRIBUTING.md` | Conventional commits, PR template, code style, testing on the live VMs |

## Monitoring

Metrics are scraped by the Prometheus/Grafana stack on the docker-stack VM:

- Prometheus: http://192.168.1.60:9091
- Grafana: http://192.168.1.60:3001

| Dashboard | Folder | What it shows |
|---|---|---|
| **WoW — Mapa ao Vivo** (`wow-live-map`) | WoW Server | link to the live map page + online stats + XY scatter of player positions |
| **WoW — Trilhas de Movimento** (`wow-movement-trails`) | WoW Server | selectable character trail (X/Y over the chosen time range) and coarse per-zone position-density heatmap |
| **WoW — Jogadores & Atividade** (`wow-players`) | WoW Server | players/accounts online over time, peak, level/class/race distribution, players by zone, most-played characters, economy |
| **WoW — Saúde do Realm** (`wow-realm-health`) | WoW Server | realm uptime + start time, DB sizes, MySQL connections, container resources, account security (failed logins, locked) |
| **Wow Server — Host & Containers** (`wow-server-host`) | WoW Server | host CPU/RAM/disk/net + per-container resources |

**Live map:** http://192.168.1.64:9400 — interactive zone map with per-player markers (class-coloured), zone picker, and an auto-refreshing player list.

Three scrape targets, all on the wow-server VM:

| Port | Exporter | Data |
|---|---|---|
| 9100 | node-exporter | host metrics |
| 8080 | cadvisor | per-container metrics |
| 9300 | **wow-exporter** (custom) | game metrics — players, characters, activity |

Setup recipe: `monitoring/docker-compose.yml`. The custom game exporter lives in
`exporters/wow-exporter/` — see `exporters/README.md` for its metric catalog and the
gotchas (MySQL 8.4 `caching_sha2_password` needs `cryptography`; `SUM()` returns
`Decimal`; `auth.uptime.starttime` is an int epoch).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the conventional commit standard, PR template, code style, and how to test against the live VMs.

This repo follows [Conventional Commits](https://www.conventionalcommits.org/) — every commit message is `<type>(<scope>): <description>` (e.g. `feat(exporter): add position metrics`). PRs use the template at `.github/PULL_REQUEST_TEMPLATE.md`.

## Architecture

```
pv1 (Proxmox @ 192.168.1.75)
├── VM 100 wow-server @ 192.168.1.64   (4 vCPU, 6 GB, 50 GB)
│   └── /opt/wow-server/   git checkout of this repo, redeployed by scripts/deploy.sh
│       ├── docker-compose.yml             compose project "wow-server"
│       │   ├── trinitycore-wowserver      :8085 world  :3724 auth  :3000 web UI  :3443 RA
│       │   ├── trinitycore-db             :3306 MySQL 8.4.4
│       │   └── chat-feed                  :9500 SSE chat stream   (tools/chat-feed)
│       └── monitoring/docker-compose.yml  compose project "monitoring"
│           ├── node-exporter              :9100 (host network)
│           ├── cadvisor                   :8080
│           ├── wow-exporter               :9300 game metrics      (exporters/wow-exporter)
│           └── wowmap                     :9400 live map + API    (tools/wowmap)
└── VM 201 docker-stack @ 192.168.1.60    (not managed by this repo)
    └── Prometheus :9091 → Grafana :3001   scrapes .64:9100, :8080, :9300
```

- **Deploys:** `/opt/wow-server` is a real clone, not a copy. `scripts/deploy.sh`
  fast-forwards it to `origin/main` and runs `docker compose up -d --build` for
  both compose projects. See `docs/DEPLOYMENT.md` → "Updating".
- **Shipping changes (read this, agents): merging a PR to `main` is all it
  takes to deploy.** A cron job on the VM runs `scripts/auto-deploy.sh` every
  5 minutes: it fetches `origin/main` and calls `scripts/deploy.sh` only when
  the remote moved (polling — GitHub webhooks can't reach a LAN IP). No manual
  step, no SSH needed after merge. Deploy history: `/var/log/wow-auto-deploy.log`
  on the VM. Details: `docs/DEPLOYMENT.md` → "Automatic deploys (cron poller)".
- **Secrets** live only in the VM's gitignored `/opt/wow-server/.env`
  (`MYSQL_ROOT_PASSWORD`, `ACCESS_PASSWORD`, `AGENT_PASSWORD`, …). Compose
  files reference them as `${VAR:?}` and fail loudly if missing. Never commit
  credentials — copy `.env.example` when setting up a new checkout.
- **AI agents:** `agent/` is the Python 3.3.5a protocol client (SRP6 auth, world
  login, chat/target actions). `Dockerfile` + `docker-compose.agents.yml` run one
  container per agent character. Agents are outbound clients of :3724/:8085 and
  publish only a read-only observability API. They run on the `wow-agents` VM,
  not on the wow-server VM, and aren't part of `scripts/deploy.sh`. See
  `docs/adr/0002-agent-host-topology.md`, `docs/AI-AGENT-SPEC.md` and
  `docs/ROADMAP.md`.

Full component and port reference: `docs/ARCHITECTURE.md`.

## Requirements

| Component | Provided by |
|-----------|-------------|
| TrinityCore 3.3.5a binaries | `danielsilvestre37/trinitycore-docker:3.3.5` |
| MySQL 8.4 | Docker service |
| Web UI | Built into the image (port 3000) |
| WoW 3.3.5a client | **User must supply** (build 12340, for map extraction) |
| TDB world dump | **User must supply** (see `tdb/README.md`) |

## Resources

- [TrinityCore](https://trinitycore.org/)
- [TrinityCore 3.3.5 Docs](https://335.trinitycore.net/)
- [GM Commands](https://trinitycore.atlassian.net/wiki/spaces/tc/pages/2130065/gm+commands)
- [valcriss/trinitycore-docker](https://github.com/valcriss/trinitycore-docker) — upstream image source
