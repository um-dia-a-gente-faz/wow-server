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
| Stack | `/opt/wow-server` (docker compose) |
| Monitoring | `/opt/monitoring` (separate compose) |

Ports: **8085** world · **3724** auth/logon · **3000** web UI. All LAN-only
(`192.168.1.0/24`), never exposed to the internet.

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
git clone git@github.com:Cividati/wow-server.git .

# 2. Client files — 17 GB, BEFORE starting (map extraction needs them)
#    From a machine that has them:
#    cd "WoW 3.3.5a" && tar czf - . | ssh root@192.168.1.64 \
#      'mkdir -p /opt/wow-server/client && cd /opt/wow-server/client && tar xzf -'

# 3. TDB (see tdb/README.md) — extract into ./tdb/

# 4. Adjust PUBLIC_IP_ADDRESS in docker-compose.yml if the VM IP differs

# 5. Start
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
  -e "s|<DATABASE_PASSWORD>|trinity|g" \
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
docker exec trinitycore-db mysql -uroot -ptrinityroot -e "SELECT id,username,expansion FROM auth.account;"
docker exec trinitycore-db mysql -uroot -ptrinityroot -e "SELECT * FROM auth.account_access;"
```

## Monitoring

Host + container metrics are exported to the existing Prometheus/Grafana on the
docker-stack VM. See `monitoring/docker-compose.yml` for the full recipe.

- Prometheus: http://192.168.1.60:9091 — jobs `node_wow`, `cadvisor_wow`
- Grafana: http://192.168.1.60:3001 — dashboard `wow-server-host`
  ("Wow Server — Host & Containers")

Managed by dropping a raw dashboard JSON into
`/opt/pandora/grafana/dashboards/` on the docker-stack VM (file provisioning, ~30s
reload). The Grafana admin password is **not** `admin:admin` — it was rotated after
first boot.

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
