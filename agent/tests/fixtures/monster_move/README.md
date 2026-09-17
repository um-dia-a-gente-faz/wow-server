# SMSG_MONSTER_MOVE fixtures

Real, live-captured `SMSG_MONSTER_MOVE` (`0x0DD`) payloads from `192.168.1.64`
(character **Luaprata**, account `AGENT01`), captured with `AGENT_DUMP_PACKETS`
during a 5-minute `--dry-run` in Sunstrider Isle (UM-64's live acceptance
check — see the PR for the full log: 80 objects tracked, 0 packets dropped
over the run). Unlike `agent/tests/fixtures/update_object/`, these are a
different packet type entirely (not `SMSG_UPDATE_OBJECT`/
`SMSG_COMPRESSED_UPDATE_OBJECT`), so they live in their own directory. Each
file is the raw application payload handed to
`agent.update_object.parse_monster_move()` — uncompressed, used as-is.

Both fixtures are the same NPC: guid `0xf13000433d0027ed`, `entry=17213`
("Broom", `world.creature_template`: `minlevel=maxlevel=1` — a wandering
Hallow's End decoration critter in Sunstrider Isle) — confirmed via a
read-only query against the live `world` database. 109 `SMSG_MONSTER_MOVE`
packets were captured in the same run; both fixtures here parsed cleanly
along with all 109 (`parse_monster_move` consumed every one to exactly
`len(payload)`), and were picked to cover the two point-count shapes seen
(73/109 packets had 1 point, 36/109 had 2 — no packet in the run needed
CatmullRom/animation/parabolic/facing-target handling, so those paths are
only covered by the hand-built fixtures in
`agent/tests/test_update_object_parser.py::MonsterMoveParseTest`).

## Files

| File | Size | `point_count` | Notes |
|---|---:|---:|---|
| `walking_npc.bin` | 49 B | 1 | The common case: `flags=0x1000` (`CanSwim` — doesn't gate any extra fields this parser reads) and no CatmullRom mask, so the single point is the raw (uncompressed) final destination — no packed-delta path exercised. |
| `walking_npc_with_waypoint.bin` | 53 B | 2 | Same NPC's next hop: 1 raw destination point + 1 packed-delta intermediate waypoint (`ByteBuffer::appendPackXYZ`'s 11/11/10-bit compressed format — see `agent.update_object._unpack_xyz_delta`). Exercises the packed-delta decode path against real bytes, not just hand-built ones. |

## Known gap: no "player walking past" fixture

`SMSG_MONSTER_MOVE` only covers NPCs. The other half of UM-64's item 3 — a
real player's `MSG_MOVE_*` broadcast (e.g. `MSG_MOVE_HEARTBEAT`) — needs a
*second* character moving near the agent. The agent itself can't produce
this: no other agent account can walk yet (movement actions are UM-38, not
built), and the same 5-minute dry-run that captured the fixtures above saw
zero `MSG_MOVE_*` packets because no other player was online nearby. See the
PR's "How to test" section for the manual live-test step (Rubens walking
near an agent) that would capture one — `agent.update_object.parse_movement_info`
and the `MSG_MOVE_OPCODES` dispatch in `agent/session.py` are covered by
hand-built unit tests in the meantime
(`agent/tests/test_update_object_parser.py::MovementInfoTest`,
`agent/tests/test_session.py::MovementBroadcastTest`).
