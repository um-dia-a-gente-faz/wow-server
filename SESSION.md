# Session log

## 2026-09-13 — build-out of the whole stack

**Hermes session ID: `20260913_204636_ceb29c`**

Searchable with `session_search` (query the id, or terms like "wow-server",
"trinitycore", "PlayerSaveInterval"). Source: `cli`. Working dir `/root/wow-server`.

What that session covered, in order:

1. **Repo + feasibility** — cloned this repo, read the docs, checked the Proxmox host.
2. **Provisioning** — created VM 100 `wow-server` @ 192.168.1.64 on pv1 (4 vCPU / 6 GB /
   50 GB), installed Docker, pulled the 3.3.5a client via magnet (17 GB), copied it in.
3. **Fixing the image's bootstrap** — two combined defects made a clean deploy
   restart-loop. Fixed by bind-mounting the exact TDB (`tdb/README.md`).
   Also hit: MySQL 8.4.4 needs `--cpu host` on the VM.
4. **Client-side** — built `~/wow-clients/` with a stock Warmane copy and a copy
   re-pointed at this server; created the GM account.
5. **Monitoring phase 1** — node-exporter + cadvisor on the VM, Prometheus targets,
   host/container Grafana dashboard.
6. **Monitoring phase 2** — custom game exporter (`exporters/`) and the
   players/realm dashboards.
7. **Live map research** — proved the world-coordinate → map-image transform
   (`tools/wowmap/transform.py`), designed the feature (`docs/LIVE-MAP.md`).

Commits from that session: `8782dd7` (initial), `e11aae7` (working deployment +
reproduce prompt), `bc65ebe` (observability).

To resume: `session_search` for the id above, then continue from `docs/LIVE-MAP.md`.
