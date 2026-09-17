# SMSG_UPDATE_OBJECT fixtures

Real, live-captured payloads from `192.168.1.64` (character **Luaprata**, account
`AGENT01`, Blood Elf Paladin, map 530 "Outland" / zone 3430 "Eversong Woods" /
area 3431 "Sunstrider Isle" per `.gps`). Each file is the raw application
payload handed to `WoWSession._parse_update_object()`: for
`SMSG_COMPRESSED_UPDATE_OBJECT` (`0x1F6`) this is the **inflated** bytes
(`zlib.decompress(payload[4:])`); for uncompressed `SMSG_UPDATE_OBJECT`
(`0x0A9`) it's the payload as-is. Both start with `uint32 block_count`.

Captured with `AGENT_DUMP_PACKETS` (UM-30) driving the agent through GM
commands sent as normal chat (`CMSG_MESSAGECHAT`, `CHAT_MSG_SAY`/type 1) —
all agent accounts are `SecurityLevel 3` on this server, so `.` commands work
in-character. **Finding worth flagging for UM-37**: sending chat with
`lang=0` (`LANG_UNIVERSAL`) gets silently dropped server-side as a "possible
hacking attempt" (`ChatHandler`); `lang=1` (`LANG_ORCISH`, the Horde default)
works. The UM-37 card's suggestion to use `LANG_UNIVERSAL` for GM accounts
does not hold on this server — verify before relying on it.

No account/session secrets are present in any fixture — these are game-state
payloads only (GUIDs, positions, health/level/entry fields).

## Files

| File | Scenario | Size | Blocks | Notes |
|---|---|---:|---:|---|
| `login_self_create.bin` | Own player's CREATE block, sent as its own small packet immediately before the burst below | 685 B | 1 | type **2** (`CREATE_OBJECT`, not `CREATE_OBJECT2`/3 — see below), guid `0x2` (Luaprata), `UPDATEFLAG_LIVING\|SELF` (`0x61`). Position `(10344.900390625, -6354.1201171875, 32.60350036621094, 0.0)` — matches `session.player_position` from `SMSG_LOGIN_VERIFY_WORLD` in the same session exactly (see UM-32 acceptance criterion "self position within 0.1yd"). `health=58, max_health=58, level=1`. |
| `login_sunstrider.bin` | First compressed burst right after login: nearby NPCs + another online player (Rubens, guid `0x1`), **not** our own self (that's the file above) | 13316 B | 55 | Block 0: guid `0x1` (Rubens, PLAYER, stationary, `health=76 max_health=76 level=2`). Blocks 1–2: two "Cat" critters (`entry=6368`, `health=1 max_health=1 level=1` — matches `world.creature_template` `minlevel=maxlevel=1`). **Block 3 has `MOVEMENTFLAG_SPLINE_ENABLED` set**, as do 13 other blocks in this burst — real spline-movement data; this is the fixture UM-64's spline create-block parser is verified against (see caveat below). |
| `busy_zone.bin` | Fresh login directly into Silvermoon City (relogin at a saved-via-`.go` position, not a same-session teleport — see caveat) | 11540 B | 74 | All 74 blocks are `CREATE_OBJECT`, all fully hand-decoded (offset lands exactly at EOF). Mix of `GAMEOBJECT` (zone banners/signage — entries 182323 "The Royal Exchange", 182324 "Court of the Sun", 182325 "Farstrider Square", 182326 "The Bazaar", 182623/182624 "Chair") and `UNIT` (`entry=37543`/`37574` "[DND] Shaker"/"Shaker - Small", `entry=25148`/`25149` "Bergrisst"/"Chief Thunder-Skins", level 60–70). All `UPDATEFLAG` values are `0x350` (GO: STATIONARY_POSITION\|LOWGUID\|POSITION... — verify against `UpdateData.h` in UM-32) or `0x60` (unit: LIVING\|SELF-ish, non-moving). No `MOVEMENT` (type 1) or `OUT_OF_RANGE_OBJECTS` (type 4) blocks present — see caveat. |
| `gameobject_cluster.bin` | A mid-size burst: 6 more "Shaker" utility NPCs near Silvermoon, all in one packet | 1216 B | 6 | All 6 blocks fully hand-decoded, offset lands exactly at EOF. All `UNIT`, `entry=37543` or `37574`, `health=3052 max_health=3052 level=60`. Good multi-block-but-small fixture (a middle ground between the two above). |
| `gameobject_create.bin` | Uncompressed `SMSG_UPDATE_OBJECT` (0xA9) for a single stationary gameobject near spawn | 93 B | 1 | type 2, guid high part `0x1FC0...`, `objectTypeId=GAMEOBJECT`, `entry=181646` ("Ship, Night Elf (Elune's Blessing)" — a docked-ship prop near the Sunstrider Isle spawn point), `UPDATEFLAG=0x252` (STATIONARY_POSITION-family, no LIVING). |
| `idle_values.bin` | `VALUES`-only block (type 0) for our own player after a GM `.modify hp 1`, mid health-regen tick | 36 B | 1 | guid `0x2` (self). Decoded fields: `health=1, max_health=1` at this particular tick (captured early in the regen ramp, before health climbed back — still a genuine natural regen tick, not the GM command's own write). Exercises `VALUES_UPDATE` mask decode (UM-33) on a small, real mask. |

## Known gap: no `OUT_OF_RANGE_OBJECTS` (type 4) or standalone `MOVEMENT` (type 1) fixture yet

Extensively attempted and root-caused, not just "didn't get around to it":

- Same-map `.go xyz` GM teleports **do not** trigger TrinityCore's live
  visibility resync (no new creates, no out-of-range) within the same
  session. Confirmed against TrinityCore 3.3.5 source
  (`src/server/game/Entities/Player/Player.cpp`, `Player::TeleportTo`,
  ~line 1642): for a same-map ("near") teleport, `SetSemaphoreTeleportNear`
  is set and `UpdatePosition()` (which drives grid/visibility) is **only**
  called once the client sends `MSG_MOVE_TELEPORT_ACK`
  (`src/server/game/Handlers/MovementHandler.cpp::HandleMoveTeleportAck`).
  Our synthetic client never sends that ack, so the live grid position never
  actually updates — only `m_teleport_dest`, which is what gets persisted to
  `characters.characters` on logout (hence `.go` *looks* like it worked when
  checked via the DB, but produces no new UPDATE_OBJECT traffic).
- Sending a synthetic `MSG_MOVE_TELEPORT_ACK` (`0x0C7`; payload is just
  `packed_guid(mover) + int32 AckIndex + int32 MoveTime` per
  `MovementPackets.cpp::MoveTeleportAck::Read()` — the handler doesn't
  validate `AckIndex`/`MoveTime` at all, only the mover GUID) was tried and
  did **not** produce a burst either in the ~10s windows tested; not fully
  root-caused why (possibly a further server tick delay, possibly another
  gate not yet found). Worth another look once UM-32 exists and a
  spline-aware ack/movement encoder is easy to build.
- `busy_zone.bin` was obtained by teleporting, then **logging out and back
  in** at the new position — a fresh login always does the full
  `AddToWorld()` sync (a different code path from `TeleportTo`), which is
  why it worked cleanly and gave a large, fully-parseable burst.
- The only OUT_OF_RANGE/MOVEMENT occurrence seen across ~8 capture sessions
  was one unexplained 2-block packet moments after a fresh login (not
  reproduced on retry); its raw bytes were not preserved.
- Real coverage of type 1/4 will most likely require either genuine
  character movement (`move_to`, Phase 2 — not built yet) or resolving the
  ack gap above. Whoever picks this up: the type-4 format is simple and
  self-delimiting (`uint32 count` + `count` packed GUIDs) so once captured
  it should be easy to add; type-1 (`MOVEMENT`) reuses the same movement-info
  layout already exercised in the CREATE blocks above.

## Caveats for whoever writes the real parser (UM-32/33)

- `login_sunstrider.bin` block 3 used to raise on spline data by design — the
  exploratory decoder used to write this README didn't implement spline
  parsing. **Resolved by UM-64**: `agent/update_object.py` now implements the
  spline create block (`_parse_create_object_spline_block`), and this fixture
  fully round-trips — see `agent/tests/test_update_object.py`'s
  `test_login_sunstrider_has_spline_blocks`. Blocks 4–54 of this file were
  never decoded *by hand* for this README, but the real parser now consumes
  all 55 to exactly `len(payload)`.
- Field decode above used the *tentative* indices from `docs/PROTOCOL-NOTES.md`
  (`OBJECT_FIELD_ENTRY=0x03`, `UNIT_FIELD_HEALTH=0x18`,
  `UNIT_FIELD_MAXHEALTH=0x20`, `UNIT_FIELD_LEVEL=0x36`) — cross-checked
  against `world.creature_template`/`world.gameobject_template` names and
  they all came back sane (level-1 "Cat" critters with 1 HP, level 60/70
  named mobs in Silvermoon, real gameobject/area names), which is a good
  independent sanity check on those indices, but UM-33 must still verify
  them against `UpdateFields.h` directly per its own card — don't treat this
  README as that citation.
- `busy_zone.bin`'s and `gameobject_cluster.bin`'s full-consumption checks
  (offset lands exactly at EOF) were done with a throwaway exploratory
  script implementing the documented movement/values layout, **not** with
  `agent/update_object.py` (doesn't exist until UM-32). Treat it as a strong
  signal the documented layout is right, not as UM-32 being pre-verified.
