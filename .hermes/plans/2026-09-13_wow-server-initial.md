# WoW 3.3.5a TrinityCore Server — Implementation Plan

> **Historical, superseded by `docs/DEPLOYMENT.md`.** This plan targeted the
> docker-stack VM (192.168.1.60). The server was actually deployed on the dedicated
> wow-server VM at **192.168.1.64**.

> **For Hermes:** Single-prompt deployment on pandora's docker-stack VM.

**Goal:** Deploy a fully functional WoW Wrath of the Lich King (3.3.5a) private
server on the pandora Proxmox host, playable from LAN clients.

**Architecture:** Docker Compose with two services — TrinityCore all-in-one
container (authserver, worldserver, web UI, bootstrap) + MySQL 8.4. Maps
extracted from user-supplied client files at first boot.

**Tech Stack:** Docker 29 + Compose v5, TrinityCore 3.3.5, MySQL 8.4,
valcriss/trinitycore-docker image.

---

## Phase 1: Bootstrap the docker-stack VM directory

### Task 1: Clone repo on pandora

```bash
ssh root@192.168.1.60 'git clone git@github.com:Cividati/wow-server.git /opt/wow-server'
```

Verify: `ssh root@192.168.1.60 'ls /opt/wow-server/docker-compose.yml'`

### Task 2: Verify Docker availability

```bash
ssh root@192.168.1.60 'docker --version && docker compose version'
```

Expected: Docker 29.x, Compose v5.x

### Task 3: Check disk space

```bash
ssh root@192.168.1.60 'df -h /opt'
```

Needs: ~20 GB free for volumes + maps + DB.

---

## Phase 2: Client data preparation

The 3.3.5a client must be placed in `/opt/wow-server/client/` BEFORE starting
the stack (or at least before the extraction phase completes).

### Task 4: Place client files

The client directory needs:
- `Wow.exe`
- `Data/` with MPQ archives
- `Data/enUS/` or equivalent locale directory

Copy from wherever the 3.3.5a client is stored:

```bash
# From local machine to pandora
scp -r /path/to/WoW_3.3.5a/* root@192.168.1.60:/opt/wow-server/client/
```

Verify: `ssh root@192.168.1.60 'ls /opt/wow-server/client/Data/enUS/*.MPQ | head -5'`

---

## Phase 3: Start and bootstrap

### Task 5: Pull images and start

```bash
ssh root@192.168.1.60 '
  cd /opt/wow-server &&
  docker compose pull &&
  docker compose up -d
'
```

### Task 6: Monitor bootstrap

```bash
ssh root@192.168.1.60 'docker compose -f /opt/wow-server/docker-compose.yml logs -f --tail 20'
```

Watch for:
1. MySQL healthy
2. Database creation
3. TDB download/import
4. Map extraction begins (this is the long step)
5. "World initialized" or authserver/worldserver started

First boot takes 30-60 minutes due to map extraction.

---

## Phase 4: Verify

### Task 7: Check services

```bash
ssh root@192.168.1.60 'docker compose -f /opt/wow-server/docker-compose.yml ps'
```

Expected: both services `Up` (healthy).

### Task 8: Check ports

```bash
ssh root@192.168.1.60 'ss -tlnp | grep -E "8085|3724|3000"'
```

Expected: three listening ports.

### Task 9: Check web UI

```bash
curl -s -o /dev/null -w "%{http_code}" http://192.168.1.60:3000
```

Expected: 200

### Task 10: Create test account

```bash
ssh root@192.168.1.60 '
  echo -e "account create testuser testpass\naccount set gmlevel testuser 3 -1\nquit" | \
  docker attach trinitycore-wowserver
'
```

Note: `docker attach` may need interactive handling. Alternative: use the web UI.

---

## Phase 5: Client connection

### Task 11: Configure client realmlist

Edit `Data/enUS/realmlist.wtf` in the WoW 3.3.5a client:

```
set realmlist 192.168.1.60
set patchlist 192.168.1.60
```

### Task 12: Connect and test

Launch `Wow.exe`, log in with created credentials, create a character.

---

## Single-Prompt Summary

```bash
# === RUN THIS ON PANDORA (ssh root@192.168.1.60) ===

# 1. Clone
git clone git@github.com:Cividati/wow-server.git /opt/wow-server
cd /opt/wow-server

# 2. Place client (skip if already done)
# scp -r WoW_3.3.5a/* root@192.168.1.60:/opt/wow-server/client/

# 3. Start
docker compose pull
docker compose up -d

# 4. Wait for bootstrap (check web UI at http://192.168.1.60:3000)
docker compose logs -f
```

---

## Risks

- **No client files**: Container will fail at extraction. Obtain a 3.3.5a client.
- **Disk space**: Map extraction needs ~5 GB temp space plus ~3 GB for extracted
  maps. Budget 20 GB total.
- **Memory**: Extraction is CPU/memory intensive. The 4 CPU / 8 GB limits should
  suffice but will slow extraction.
- **SSH key on pandora**: The docker-stack VM needs an SSH key registered with
  GitHub to clone the private repo. If missing, generate one and add to
  Cividati's GitHub account, or use `git clone` with a token.