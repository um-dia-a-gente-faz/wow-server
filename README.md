# World of Warcraft — Wrath of the Lich King (3.3.5a) Private Server

TrinityCore-based WoW server, Dockerized for single-command deployment on the
pandora Proxmox host (VM 201, docker-stack).

## Quick Start (on pandora)

```bash
# 1. Clone this repo on the docker-stack VM
git clone git@github.com:Cividati/wow-server.git /opt/wow-server
cd /opt/wow-server

# 2. Place your 3.3.5a client in ./client/
#    Copy the full WoW directory (Data/, Wow.exe, etc.) into ./client/

# 3. Start everything
docker compose up -d

# 4. Watch bootstrap progress in the web UI
#    http://192.168.1.60:3000
```

First boot extracts maps (dbc, maps, vmaps, mmaps) from the client — this takes
30-60 minutes depending on CPU. The web UI shows live progress.

Once complete:
- World server: `192.168.1.60:8085`
- Auth server:  `192.168.1.60:3724`
- Web UI:      `http://192.168.1.60:3000`

## Connecting

Edit `Data/enUS/realmlist.wtf` in your 3.3.5a client:

```
set realmlist 192.168.1.60
```

Create an account via the worldserver console:

```bash
docker attach trinitycore-wowserver
# (press Enter for the `TC>` prompt)
account create <username> <password>
account set gmlevel <username> 3 -1
# Ctrl+P, Ctrl+Q to detach
```

Or use the web UI at `http://192.168.1.60:3000`.

## Architecture

```
pandora (Proxmox pv1)
└── VM 201 (docker-stack @ 192.168.1.60)
    └── /opt/wow-server/
        ├── docker-compose.yml
        ├── client/          ← WoW 3.3.5a client (user-supplied)
        ├── server_data/     ← extracted maps (Docker volume)
        ├── server_logs/     ← TrinityCore logs
        └── db_data/         ← MySQL data
```

## Requirements

| Component | Provided by |
|-----------|-------------|
| TrinityCore 3.3.5a binaries | `danielsilvestre37/trinitycore-docker:3.3.5` |
| MySQL 8.4 | Docker service |
| Web UI | Built into the image (port 3000) |
| WoW 3.3.5a client | **User must supply** (map extraction) |

## Resources

- [TrinityCore](https://trinitycore.org/)
- [TrinityCore 3.3.5 Docs](https://335.trinitycore.net/)
- [GM Commands](https://trinitycore.atlassian.net/wiki/spaces/tc/pages/2130065/gm+commands)
- [valcriss/trinitycore-docker](https://github.com/valcriss/trinitycore-docker) — upstream image source