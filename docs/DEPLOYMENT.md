# Deployment

Single-command deployment on the pandora docker-stack VM.

## Prerequisites

- Docker 29+ and Compose v5 (already on VM 201)
- ~20 GB free disk space
- WoW 3.3.5a (build 12340) client files

## One-Shot Deploy

SSH into the docker-stack VM and run:

```bash
ssh root@192.168.1.60

# Clone and deploy
git clone git@github.com:Cividati/wow-server.git /opt/wow-server
cd /opt/wow-server

# Place client files BEFORE starting (required for map extraction)
# Copy your WoW 3.3.5a directory to ./client/
# E.g.: scp -r WoW_335a/* root@192.168.1.60:/opt/wow-server/client/

# Verify client is present
ls ./client/Data/enUS/  # should show locale-*.MPQ files

# Start
docker compose up -d

# Watch bootstrap progress
docker compose logs -f
```

## The Single-Prompt Command

From any Hermes session targeting pandora, this single command does everything:

```bash
ssh root@192.168.1.60 '
  cd /opt/wow-server &&
  git pull origin main &&
  docker compose up -d &&
  echo "Waiting for bootstrap..." &&
  sleep 10 &&
  docker compose logs --tail 20
'
```

If the client directory is NOT yet populated, the container will wait at the
extraction phase. Place the client files and restart:

```bash
ssh root@192.168.1.60 '
  cd /opt/wow-server &&
  docker compose restart trinitycore-wowserver &&
  docker compose logs -f
'
```

## Verification

```bash
# Check services are running
ssh root@192.168.1.60 'docker compose -f /opt/wow-server/docker-compose.yml ps'

# Check worldserver is listening
ssh root@192.168.1.60 'ss -tlnp | grep -E "8085|3724|3000"'

# Check web UI
curl -s -o /dev/null -w "%{http_code}" http://192.168.1.60:3000
# Expected: 200

# Check world server accepts connections
echo "quit" | timeout 3 nc 192.168.1.60 8085 2>/dev/null && echo "port open" || echo "check logs"
```

## Management

```bash
# View logs
ssh root@192.168.1.60 'docker compose -f /opt/wow-server/docker-compose.yml logs -f --tail 50'

# Attach to worldserver console (GM commands)
ssh root@192.168.1.60 'docker attach trinitycore-wowserver'
# Ctrl+P Ctrl+Q to detach

# Restart
ssh root@192.168.1.60 'docker compose -f /opt/wow-server/docker-compose.yml restart'

# Stop
ssh root@192.168.1.60 'docker compose -f /opt/wow-server/docker-compose.yml down'

# Full reset (WARNING: deletes all data)
ssh root@192.168.1.60 '
  cd /opt/wow-server &&
  docker compose down -v &&
  rm -rf server_data/ server_logs/
  docker compose up -d
'
```

## Resource Limits

Configured in docker-compose.yml:
- wowserver: 4 CPU max, 8 GB RAM max (2 CPU / 4 GB reserved)
- MySQL: no explicit limits (typically ~1 GB)

Adjust in docker-compose.yml if needed, then `docker compose up -d`.