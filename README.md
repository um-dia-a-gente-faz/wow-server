# World of Warcraft — Wrath of the Lich King (3.3.5a) Private Server

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
| `docs/DEPLOYMENT.md` | Actual deployment, gotchas, debugging, troubleshooting table |
| `docs/CLIENT-SETUP.md` | Client configuration and troubleshooting |
| `docs/ARCHITECTURE.md` | Component and network layout |
| `docs/GM-COMMANDS.md` | Useful in-game GM commands |
| `docs/LIVE-MAP.md` | Live map: how it was built, DBC field order, extraction, alignment notes |
| `docs/ROADMAP.md` | Next features: agent panel, chat, inspect, trails, calibration |
| `exporters/README.md` | Custom game metrics exporter: catalog, build, and its gotchas |
| `grafana/` | Dashboard provisioning config — the file-based loading that replaced API auth |
| `tdb/README.md` | Which TDB version to use and why |
| `SESSION.md` | Hermes session id for this build-out, and what it covered |
| `tools/wowmap/` | World-coordinate → map-image transform (reads the extracted DBCs) |

## Monitoring

Metrics are scraped by the Prometheus/Grafana stack on the docker-stack VM:

- Prometheus: http://192.168.1.60:9091
- Grafana: http://192.168.1.60:3001

| Dashboard | Folder | What it shows |
|---|---|---|
| **WoW — Mapa ao Vivo** (`wow-live-map`) | WoW Server | link to the live map page + online stats + XY scatter of player positions |
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
└── VM 100 wow-server @ 192.168.1.64   (4 vCPU, 6 GB, 50 GB)
    ├── /opt/wow-server/   trinitycore-wowserver + trinitycore-db
    └── /opt/monitoring/   node-exporter + cadvisor
```

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
