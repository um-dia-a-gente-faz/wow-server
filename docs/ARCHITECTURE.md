# Architecture

## Overview

```
┌──────────────────────────────────────────────────────────────┐
│              Proxmox host pv1 (192.168.1.75)                 │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │   VM 100 — wow-server (192.168.1.64)                   │  │
│  │   Ubuntu 24.04, Docker 29, 4 vCPU / 6 GB / 50 GB       │  │
│  │                                                        │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  /opt/wow-server/  (docker compose)              │  │  │
│  │  │  ┌────────────────────────────────────────────┐  │  │  │
│  │  │  │ trinitycore-wowserver                      │  │  │  │
│  │  │  │   :8085 world   :3724 auth   :3000 web UI  │  │  │  │
│  │  │  │   /app/client  ← ./client/   (extraction)  │  │  │  │
│  │  │  │   /app/server/bin/TDB_full_world_*.sql ← ./tdb/  │  │  │
│  │  │  │   /app/server/data, /app/server/logs (vols)│  │  │  │
│  │  │  └────────────────────────────────────────────┘  │  │  │
│  │  │                    │ depends_on                  │  │  │
│  │  │                    ▼                             │  │  │
│  │  │  ┌────────────────────────────────────────────┐  │  │  │
│  │  │  │ trinitycore-db (mysql:8.4.4)               │  │  │  │
│  │  │  │   db_data volume                           │  │  │  │
│  │  │  └────────────────────────────────────────────┘  │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │                                                        │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  /opt/monitoring/  (docker compose, separate)    │  │  │
│  │  │   node-exporter :9100   cadvisor :8080           │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │   VM 201 — docker-stack (192.168.1.60)                 │  │
│  │   Prometheus :9091  →  Grafana :3001  →  Caddy :80     │  │
│  │   scrapes 192.168.1.64:9100 + :8080                    │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
          ▲
          │ LAN 192.168.1.0/24  (no external exposure)
          │
  ┌───────┴────────┐
  │  WoW client    │
  │  3.3.5a        │
  │  realmlist →   │
  │  192.168.1.64  │
  └────────────────┘
```

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

Image: `danielsilvestre37/trinitycore-docker:3.3.5`
Source: https://github.com/valcriss/trinitycore-docker

### trinitycore-db

MySQL 8.4.4 storing:

- `auth` — accounts, realm list, RBAC, logs
- `characters` — player characters, inventory, quests
- `world` — NPCs, items, spells, quests, game objects (populated from the TDB dump)

> MySQL 8.4.4 requires the x86-64-v2 CPU baseline. The Proxmox VM must be created with
> `--cpu host`; the default `kvm64` model lacks it and the container crash-loops.

### Volumes

| Volume | Purpose | Persists |
|---|---|---|
| `server_data` | Extracted maps (dbc, maps, vmaps, mmaps) | Yes — ~2.9 GB |
| `server_logs` | TrinityCore runtime logs | Yes |
| `db_data` | MySQL data files | Yes |
| `./client` | WoW 3.3.5a client (bind mount, read for extraction) | User-supplied, ~17 GB |
| `./tdb/*.sql` | TDB world dump (bind mount) | User-supplied, ~280 MB |
| `/app/tmp` | tmpfs for extraction temp files | Ephemeral |

`/app/server/bin` is **not** a volume — anything placed there is lost when the
container is recreated. That is why the TDB file is bind-mounted rather than copied.

## Network

All traffic is LAN-only. No external exposure, no TLS.

| Port | Service | Protocol |
|------|---------|----------|
| 3724 | Auth server | TCP (WoW login protocol) |
| 8085 | World server | TCP (WoW game protocol) |
| 3000 | Web UI | HTTP (management + console) |
| 9100 | node-exporter | HTTP (Prometheus scrape) |
| 8080 | cadvisor | HTTP (Prometheus scrape) |

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
