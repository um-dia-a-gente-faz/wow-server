# Foot below ground and wall-staring: live diagnosis (partial)

> **Spike, partial.** Outcome: sidecar measured; the two symptoms are not yet diagnosed on the live realm. Issue: #847 (split from #313, which stays open and parked for steps 2–3). Superseded by: nothing. No code fix is proposed because no evidence supports one yet.

## What blocked the rest

The author session could not log in an agent. `ssh root@192.168.1.64` fails with `Host key verification failed` (the VM is not in `~/.ssh/known_hosts`), so `WOW_PASSWORD` and the audit logs in `/opt/wow-server-metrics/audit` were out of reach. Adding the key unprompted would bypass the host check, so it was not done. Steps 2 and 3 of the #313 Decision need a session whose host already trusts the VM.

## 1. Sidecar on real data (map 530, Sunstrider Isle start, 10349.6 / -6357.3)

Queries went to `http://192.168.1.64:8770`, 3 to 4 per second at most, from the author host (LAN, so network time is small). z of both ends came from `GET /height`.

| Probe | Result |
|---|---|
| 20 random pairs within 120 yd of the start | 17 queried (3 had an end off the mesh). p50 635 ms, p95 932 ms, max 991 ms. 15 `ok` (2 to 7 corners), 2 `no_path` |
| 18 paths of 100 to 800 yd from the start, 3 directions | 12 `ok`, 5 end points off the mesh, 1 `no_path` (800 yd west). Latency 0.62 to 1.38 s |
| Same 60 yd path, repeated 4 times (warm) | 512 to 521 ms each |
| Start to Silvermoon (10349,-6357 to 9921,-7227) | `no_path` after 898 ms |
| `/healthz` afterwards | `{"ok": true, "maps_loaded": [530]}` |

Reading:

- Latency is **not** dominated by tile loading: a warm 60 yd path still takes about 520 ms, and it grows with distance (about 1.2 to 1.4 s at 300 to 800 yd). `PathClient` times out at 2.0 s (`agent/pathing.py`). One slow query on a busy VM turns into a `path_fallback` (straight line), so the margin is under 1.5x for long routes. Follow-up: #848.
- `no_path` from the isle to Silvermoon is unexplained. Candidates: the 4096-poly `max_polys` cap in `tools/pathfinder/navmesh.py` (`find_path`) on a long route, a missing tile, or a real gap in the data. Not separated yet. This also covers #816's open human step ("path across a real tile border"): short paths succeed across the 400 to 800 yd probes, which cross at least one tile border (533 yd tiles), but only the one-direction sample is evidence, not a controlled check.
- About 1 in 4 random points returned `null` from `/height` (water, cliffs or unbaked). A `move_to` aimed there gets `no_path`, which `Mover._move_routed` returns as `error: "no path"`.

## 2. Foot below ground

Not measured (needs a logged-in agent). What the code says, so the measurement can be targeted:

- `Mover._move_routed` walks the sidecar's corners and gives each segment its own `_simulate` call, which interpolates z **linearly** between the segment's two ends. Corner z is terrain height (#312), so z is exact at corners and only approximate between them.
- Funnel corners on a slope can be far apart (the 800 yd probe returned 10 corners, about 80 yd per segment), so a hill between two corners is the case to test.
- `Mover.move_towards` never uses the sidecar: it is `_straight` with the target's perceived z. Chasing or approaching an NPC (`move_towards`, `death.py` spirit healer) has no terrain z at all.
- A fix by `/height` per tick at 10 ticks per second would exceed the 5 req/s budget and the measured 0.5 s latency; that needs a Decision (for example a height call every N yards, or longer corridors) before any code.

To measure: log `session.player_position` z at each heartbeat and `GET /height` at that x/y (at most 5 per second), with and without `AGENT_PATHFINDER_URL`, and report max and mean |dz|.

## 3. Wall-staring

Not diagnosed (needs the audit and decision logs). Findings from the code that narrow it:

- `_simulate` is client-authoritative and has no collision. "Stuck" is measured on the **simulated** position, which always advances, so `no_progress` cannot fire against a wall. Only `server_drift` can, and only when the server position changed during the move.
- `move_towards` is always straight-line and `candidates.py` offers only short hops for it, so a target behind a wall is approached through the wall. The unit that matters is whether the server accepted those positions.
- `_corpse_run` in `agent/death.py` handles "stuck" with angled detours, but the first `move_to` is routed only when a sidecar is configured. Without `AGENT_PATHFINDER_URL` (all of `wow-agents` today, per the #313 Decision) there is no path, so the original symptom cannot be re-verified there.
- With the sidecar on, `no path` is a clean failure the brain sees; whether the brain then re-picks the same unreachable target (the stare) is what the decision log would show.

Questions the logs must answer: was it `no path`, no candidate, or an action that never reported stuck?

## Next

1. A session whose host trusts the VM runs steps 2 and 3 of the #313 Decision.
2. #848 investigates latency and the Silvermoon and 800 yd west `no_path` cases (cap or data).
3. Human step on #313: re-verify both symptoms with the fleet once `wow-agents` has `AGENT_PATHFINDER_URL`.
