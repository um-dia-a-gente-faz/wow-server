# Architecture

## Overview

```
┌─────────────────────────────────────────────────┐
│           Proxmox Host (pv1, 192.168.1.75)      │
│                                                  │
│  ┌────────────────────────────────────────────┐ │
│  │   VM 201 — docker-stack (192.168.1.60)     │ │
│  │   Ubuntu 24.04, Docker 29, Compose v5      │ │
│  │                                            │ │
│  │  ┌──────────────────────────────────────┐  │ │
│  │  │  /opt/wow-server/                    │  │ │
│  │  │                                      │  │ │
│  │  │  docker compose:                     │  │ │
│  │  │  ┌──────────────────────────────┐    │  │ │
│  │  │  │ trinitycore-wowserver        │    │  │ │
│  │  │  │  :8085 (world)               │    │  │ │
│  │  │  │  :3724 (auth/logon)          │    │  │ │
│  │  │  │  :3000 (web UI)              │    │  │ │
│  │  │  │  /app/client  ← ./client/    │    │  │ │
│  │  │  │  /app/server/data (volume)    │    │  │ │
│  │  │  │  /app/server/logs (volume)   │    │  │ │
│  │  │  └──────────────────────────────┘    │  │ │
│  │  │         │ depends_on                  │  │ │
│  │  │         ▼                             │  │ │
│  │  │  ┌──────────────────────────────┐    │  │ │
│  │  │  │ trinitycore-db (mysql:8.4)   │    │  │ │
│  │  │  │  /var/lib/mysql (volume)     │    │  │ │
│  │  │  └──────────────────────────────┘    │  │ │
│  │  └──────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
          ▲
          │ LAN (192.168.1.0/24)
          │
  ┌───────┴───────┐
  │  WoW Client   │
  │  3.3.5a       │
  │  realmlist →  │
  │  192.168.1.60 │
  └───────────────┘
```

## Components

### trinitycore-wowserver

Single container bundling:
- **authserver** — handles login authentication on port 3724
- **worldserver** — game world simulation on port 8085
- **Web UI** — browser-based management on port 3000
- **Bootstrap scripts** — auto-creates databases, downloads TDB seed, applies updates
- **Map extractors** — extracts dbc, maps, vmaps, mmaps from the client directory

Image: `danielsilvestre37/trinitycore-docker:3.3.5`
Source: https://github.com/valcriss/trinitycore-docker

### trinitycore-db

MySQL 8.4 database storing:
- `auth` — accounts, realm list, logs
- `characters` — player characters, inventory, quests
- `world` — NPCs, items, spells, quests, game objects

### Volumes

| Volume | Purpose | Persists |
|--------|---------|----------|
| `server_data` | Extracted maps (dbc, maps, vmaps, mmaps) | Yes |
| `server_logs` | TrinityCore runtime logs | Yes |
| `db_data` | MySQL data files | Yes |
| `./client` | WoW 3.3.5a client (bind mount, read for extraction) | User-supplied |
| `/app/tmp` | tmpfs for extraction temp files | Ephemeral |

## Network

All traffic is LAN-only (192.168.1.0/24). No external exposure.

| Port | Service | Protocol |
|------|---------|----------|
| 3724 | Auth server | TCP (WoW login protocol) |
| 8085 | World server | TCP (WoW game protocol) |
| 3000 | Web UI | HTTP (browser management) |

## Bootstrap Sequence

On first `docker compose up -d`:
1. MySQL starts and becomes healthy
2. Main container starts, waits for MySQL
3. Creates `auth`, `characters`, `world` databases if missing
4. Downloads TDB (Trinity Database) seed if not present
5. Applies schema and updates
6. Configures realm with `PUBLIC_IP_ADDRESS`
7. Extracts maps from `./client/` (this takes 30-60 min)
8. Starts authserver and worldserver
9. Web UI becomes available at :3000

On subsequent starts: steps 1-9 are skipped; authserver + worldserver start immediately.