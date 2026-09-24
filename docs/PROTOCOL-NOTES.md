# 3.3.5a protocol notes

Wire-format notes for the `agent/` client, **each checked against TrinityCore
branch `3.3.5`** (paths relative to `src/server/game/`). Only add a line here if
you checked it against that source too, and name the file it came from. The
perception work that consumes this is tracked in Linear **UM-32** (block framing +
movement block) and **UM-33** (VALUES_UPDATE mask + field mapping).

All integers are little-endian.

## Opcodes (`Server/Protocol/Opcodes.h`)

| Opcode | Value |
|---|---|
| `SMSG_UPDATE_OBJECT` | `0x0A9` |
| `SMSG_COMPRESSED_UPDATE_OBJECT` | `0x1F6` |

> `agent/session.py` currently defines `SMSG_UPDATE_OBJECT = 0x1F7`, which is
> `SMSG_PLAY_SPELL_IMPACT` in `Opcodes.h`. Uncompressed update packets are sent
> for payloads ≤ 100 bytes (`UpdateData::BuildPacket`), so those are missed until
> the constant is fixed.

`SMSG_COMPRESSED_UPDATE_OBJECT` payload = `uint32 uncompressedSize` + a zlib stream
(`deflateInit`, so `zlib.decompress(payload[4:])`). The inflated bytes have the
same layout as `SMSG_UPDATE_OBJECT`.

## SMSG_UPDATE_OBJECT framing (`Entities/Object/Updates/UpdateData.cpp`, `Object.cpp`)

```
uint32 blockCount            // includes the out-of-range block, if present
repeat blockCount:
  uint8 updateType
  ...                        // shape depends on updateType, below
```

`updateType` (`Updates/UpdateData.h`): `0` VALUES, `1` MOVEMENT, `2` CREATE_OBJECT,
`3` CREATE_OBJECT2, `4` OUT_OF_RANGE_OBJECTS, `5` NEAR_OBJECTS.

| Block | Layout after `updateType` | Source |
|---|---|---|
| VALUES (0) | packed GUID, **values update**. No update flags, no movement data. | `Object::BuildValuesUpdateBlockForPlayer` |
| CREATE_OBJECT / CREATE_OBJECT2 (2/3) | packed GUID, `uint8 objectTypeId`, **movement update**, **values update** | `Object::BuildCreateUpdateBlockForPlayer` |
| OUT_OF_RANGE_OBJECTS (4) | `uint32 count`, then `count` packed GUIDs. Written first, straight after `blockCount`. | `UpdateData::BuildPacket` |

## Movement update (`Object::BuildMovementUpdate`)

Starts with `uint16 updateFlags` (`Updates/UpdateData.h`):

| Flag | Value |
|---|---|
| `UPDATEFLAG_SELF` | `0x0001` |
| `UPDATEFLAG_TRANSPORT` | `0x0002` |
| `UPDATEFLAG_HAS_TARGET` | `0x0004` |
| `UPDATEFLAG_UNKNOWN` | `0x0008` |
| `UPDATEFLAG_LOWGUID` | `0x0010` |
| `UPDATEFLAG_LIVING` | `0x0020` |
| `UPDATEFLAG_STATIONARY_POSITION` | `0x0040` |
| `UPDATEFLAG_VEHICLE` | `0x0080` |
| `UPDATEFLAG_POSITION` | `0x0100` |
| `UPDATEFLAG_ROTATION` | `0x0200` |
| `UPDATEFLAG_NO_BIRTH_ANIM` | `0x0400` |

Then at most one position form, checked in this order:

- **`LIVING`**: movement info (`uint32 moveFlags`, `uint16 moveFlags2`,
  `uint32 time`, `float x, y, z, o`, then conditional transport / pitch / fall /
  jump / spline-elevation fields), then 9 `float` speeds, then spline data if
  `MOVEMENTFLAG_SPLINE_ENABLED`. See `Unit::BuildMovementPacket`.
- **else `POSITION`**: packed transport GUID (a single `0x00` byte when not on a
  transport), `float x, y, z`, `float x, y, z` again (transport offset instead when
  on a transport), `float o`, `float o` again (transport `o` instead).
- **else `STATIONARY_POSITION`**: `float x, y, z, o`.

Then these trailing fields, in this order, each only when its flag is set:
`UNKNOWN` `uint32`, `LOWGUID` `uint32`, `HAS_TARGET` packed GUID, `TRANSPORT`
`uint32`, `VEHICLE` `uint32` + `float`, `ROTATION` `int64`.

## Spline create block (`WorldPackets::Movement::CommonMovement::WriteCreateObjectSplineDataBlock`, `Server/Packets/MovementPackets.cpp`)

Appended to a `LIVING` movement block (after the 9 speed floats) when
`MOVEMENTFLAG_SPLINE_ENABLED` (`0x08000000`) is set — a moving NPC or player,
caught mid-`CREATE`. Verified against the live `TrinityCore/TrinityCore`
branch `3.3.5` (`gh api repos/TrinityCore/TrinityCore/contents/... ?ref=3.3.5`)
and empirically against `login_sunstrider.bin`'s 14 real spline blocks — the
whole 13316 B payload parses to exactly `len(payload)` with this layout.

```
uint32 splineFlags                  // Movement::MoveSplineFlag
// at most one, gated by Mask_Final_Facing (Final_Point|Final_Target|Final_Angle):
float finalAngle                    // if Final_Angle (0x00020000)
uint64 finalTargetGuid              // if Final_Target (0x00010000) — RAW guid, not packed (ByteBuffer operator<<(ObjectGuid))
float finalPoint.x, y, z            // if Final_Point (0x00008000)
int32  timePassed                   // elapsed ms
uint32 duration                     // total ms
uint32 splineId
float  durationMod, nextDurationMod // always 1.0f on the wire, unused
float  verticalAcceleration
uint32 effectStartTime
uint32 pointCount
float  points[pointCount].x, y, z   // MoveSpline::getPath() verbatim — raw floats, no packed-delta compression here
uint8  mode                         // Spline::EvaluationMode: 0 linear, 1 catmullrom, 2 unused bezier3
float  destination.x, y, z          // (0,0,0) if the spline is cyclic
```

## `SMSG_MONSTER_MOVE` (`WorldPackets::Movement::MonsterMove::Write`/`InitializeSplineData`, `MovementPackets.cpp`)

An NPC's new destination/path. Does **not** cover `SMSG_MONSTER_MOVE_TRANSPORT`
(`0x2AE`, transport-relative movement) — different, unimplemented layout.

```
packedGuid mover
uint8  vehicleExitVoluntary
float  pos.x, y, z                  // starting position, no orientation
uint32 splineId
uint8  moveType                     // MonsterMoveType: 0 NORMAL, 1 STOP, 2 FACING_SPOT, 3 FACING_TARGET, 4 FACING_ANGLE
// if moveType != STOP:
  // at most one, gated by moveType:
  uint64 faceGuid                   // if FACING_TARGET — raw, not packed
  float  faceDirection               // if FACING_ANGLE
  float  faceSpot.x, y, z            // if FACING_SPOT
  uint32 flags                       // Movement::MoveSplineFlag
  uint8  animTier; uint32 animStartTime   // if flags & Animation (0x00200000)
  uint32 moveTime
  float  jumpGravity; uint32 jumpStartTime // if flags & Parabolic (0x00000800)
  uint32 pointCount
  // if flags & (Flying|Catmullrom) (0x00002000|0x00040000): pointCount raw float x,y,z points
  // else: 1 raw float x,y,z point (the final destination), then (pointCount-1) packed-delta uint32s —
  //       each a compressed offset from the midpoint of start and destination (ByteBuffer::appendPackXYZ:
  //       11/11/10-bit signed fields, quarter-yard units, low-to-high x/y/z)
```

## `MSG_MOVE_*` broadcasts (`WorldPackets::Movement::MoveUpdate::Write`, `MovementPackets.cpp`)

Another player's movement (start/stop forward/strafe/turn, jump, fall-land,
swim, set-facing, heartbeat, ...) relayed to nearby clients:
`WorldSession::HandleMovementOpcodes` in `Opcodes.cpp` lists every opcode that
uses this exact wire shape.

```
packedGuid mover
MovementInfo                        // same fields as the LIVING form above, MINUS the 9 speeds and spline data —
                                     // those are UPDATE_OBJECT-specific; a plain MSG_MOVE_* packet ends right here
```

## Values update (`Updates/UpdateMask.h`, `Object::BuildValuesUpdate`)

```
uint8  maskBlockCount
uint32 maskBlocks[maskBlockCount]   // bit N set => field N present
uint32 value                        // one per set bit, ascending field index
```

## Field indices (`Entities/Object/Updates/UpdateFields.h`)

Unit fields are written as `OBJECT_END + n` in the header, and player fields as
`UNIT_END + n`. The table gives absolute indices. GUID-typed fields (`Size: 2`)
take two words.

| Field | Index |
|---|---|
| `OBJECT_FIELD_GUID` | `0x00` (2 words) |
| `OBJECT_FIELD_TYPE` | `0x02` |
| `OBJECT_FIELD_ENTRY` | `0x03` |
| `OBJECT_FIELD_SCALE_X` | `0x04` (float) |
| `OBJECT_END` | `0x06` |
| `UNIT_FIELD_HEALTH` | `0x18` |
| `UNIT_FIELD_MAXHEALTH` | `0x20` |
| `UNIT_FIELD_LEVEL` | `0x36` |
| `UNIT_FIELD_FACTIONTEMPLATE` | `0x37` |
| `UNIT_NPC_FLAGS` | `0x52` |
| `UNIT_END` | `0x94` |
| `PLAYER_FLAGS` | `0x96` |

## Name resolution: `CMSG_NAME_QUERY` / `CMSG_CREATURE_QUERY` / `CMSG_GAMEOBJECT_QUERY` (`Handlers/QueryHandler.cpp`, `Server/Packets/QueryPackets.h`/`.cpp`)

Verified against the live `TrinityCore/TrinityCore` branch `3.3.5` (UM-35;
`gh api repos/TrinityCore/TrinityCore/contents/... ?ref=3.3.5`).

Opcodes: `CMSG_NAME_QUERY 0x050` → `SMSG_NAME_QUERY_RESPONSE 0x051`,
`CMSG_GAMEOBJECT_QUERY 0x05E` → `SMSG_GAMEOBJECT_QUERY_RESPONSE 0x05F`,
`CMSG_CREATURE_QUERY 0x060` → `SMSG_CREATURE_QUERY_RESPONSE 0x061`.

```
CMSG_NAME_QUERY:        uint64 guid                    // raw, NOT packed (QueryPlayerName::Read)
CMSG_CREATURE_QUERY:    uint32 entry, uint64 guid       // guid is a sample instance; only entry is looked up
CMSG_GAMEOBJECT_QUERY:  uint32 entry, uint64 guid       // ditto
```

`SMSG_NAME_QUERY_RESPONSE` (`QueryPlayerNameResponse::Write`):
```
packedGuid player                 // PACKED here, unlike the request's raw guid
uint8 result                       // 0 = full data follows, non-zero = not found
// if result == 0:
cstring name
cstring realmName
uint8 race, uint8 sex, uint8 classId
uint8 hasDeclinedNames             // if 1, 5 more cstrings follow (Cyrillic client feature) — not parsed
```

`SMSG_CREATURE_QUERY_RESPONSE` (`QueryCreatureResponse::Write`, `CreatureData.h` for the `MAX_*` constants):
```
uint32 (entry | (found ? 0 : 0x80000000))
// if found:
cstring name, uint8 x3 (name2/3/4, always empty), cstring subname (Title), cstring cursorName
uint32 flags, uint32 creatureType, uint32 creatureFamily, uint32 classification (rank)
uint32 killCredit[2], uint32 displayId[4]
float hpMulti, float energyMulti, uint8 leader
uint32 questItems[6], uint32 movementInfoId
```
`classification` (`CreatureEliteType`, SharedDefines.h): `0` normal, `1` elite,
`2` rareelite, `3` worldboss, `4` rare, `5` trivial. `creatureType`
(`CreatureType`, SharedDefines.h): `1` beast .. `13` gas_cloud — see
`agent/names.py::CREATURE_TYPE_NAMES` for the full mapping.

`SMSG_GAMEOBJECT_QUERY_RESPONSE` (`QueryGameObjectResponse::Write`, `GameObjectData.h`/`SharedDefines.h` for `MAX_GAMEOBJECT_DATA=24`):
```
uint32 (entry | (found ? 0 : 0x80000000))
// if found:
uint32 type, uint32 displayId
cstring name, uint8 x3 (name2/3/4, always empty)
cstring iconName, cstring castBarCaption, cstring unkString
uint32 data[24]                    // type-specific params, not decoded
float size
uint32 questItems[6]
```

All the `cstring` fields above (`name`, `subname`, `realmName`, ...) are
plain null-terminated strings with **no length prefix** — `ByteBuffer::
operator<<(std::string)` (`ByteBuffer.h`), same as `SMSG_MESSAGECHAT`'s
channel name, and different from that packet's `senderName`/`chatText`
(`uint32` length prefix). `agent/packets.py::cstring`.

## Sending movement: `CMSG_SET_ACTIVE_MOVER` is required before any `MSG_MOVE_*` we send takes effect

Found live-verifying UM-36's `face` action: a `MSG_MOVE_SET_FACING` (or any other
`MSG_MOVE_*`) packet we send is **silently dropped** unless the agent has
first sent `CMSG_SET_ACTIVE_MOVER` (`0x26A`, payload: raw uint64 guid —
`WorldSession::HandleSetActiveMoverOpcode`, `MovementHandler.cpp`) for its
own guid, once, after login.

Why: `WorldSession::HandleMovementOpcode` (`MovementHandler.cpp`) calls
`ValidateAndGetUnitBeingMoved(movementInfo.guid, opcode, false)`, which
requires `GameClient::GetActivelyMovedUnit()` to be non-null and match the
guid in the packet. That field is *only* ever set by
`HandleSetActiveMoverOpcode` — nothing sets it automatically at login. A
real client sends `CMSG_SET_ACTIVE_MOVER` for itself as part of its normal
post-login sequence; our headless client didn't, so every movement packet
we sent (facing included) was accepted at the socket level, parsed, and
then dropped with no error response — `ValidateAndGetUnitBeingMoved` logs a
`TC_LOG_DEBUG` on rejection, but nothing at `INFO` level, so this was
invisible without live testing.

Fix: `agent/session.py::login_character` sends `CMSG_SET_ACTIVE_MOVER` with
our own guid right after `SMSG_LOGIN_VERIFY_WORLD`, before the recv thread
starts. Verified live: `face`'s resulting orientation matched exactly (to 6
decimal places) between the packet we sent and `characters.characters.orientation`
after a save. This also unblocks UM-38 (movement) — the same gate applies
to every `MSG_MOVE_*` opcode.

## `SMSG_MESSAGECHAT` / `SMSG_GM_MESSAGECHAT` (`WorldPackets::Chat::Chat::Write`, `Server/Packets/ChatPackets.cpp`)

Verified against the live `TrinityCore/TrinityCore` branch `3.3.5` (UM-66;
`gh api repos/TrinityCore/TrinityCore/contents/... ?ref=3.3.5`).

```
uint8  slashCmd            // ChatMsg (SharedDefines.h) — see agent/session.py::CHAT_KIND_NAMES for the full enum
int32  language
uint64 senderGuid          // raw ObjectGuid, NOT packed (ByteBuffer operator<<(ObjectGuid))
uint32 flags                // always 0 in 3.3.5
```

Then one of four shapes, selected by `slashCmd`:

| Group | Shape |
|---|---|
| `CHAT_MSG_MONSTER_SAY/PARTY/YELL/WHISPER/EMOTE`, `RAID_BOSS_EMOTE/WHISPER`, `BATTLENET` | `uint32 len + senderName` (always — an NPC has no other way for the client to know its name), `uint64 targetGuid`, then `uint32 len + targetName` **only if** `targetGuid != 0` and its `HighGuid` (top 16 bits) is neither `Player` (`0x0000`) nor `Pet` (`0xF140`) |
| `CHAT_MSG_WHISPER_FOREIGN` | `uint32 len + senderName`, `uint64 targetGuid` (no conditional target name) |
| `CHAT_MSG_BG_SYSTEM_NEUTRAL/ALLIANCE/HORDE` | `uint64 targetGuid`, then `uint32 len + targetName` **only if** `targetGuid != 0` and not a player |
| everything else (default — say/yell/whisper/party/guild/officer/emote/text_emote/channel/achievement/…) | `[uint32 len + senderName]` only on the `SMSG_GM_MESSAGECHAT` opcode, `[channel` as a plain null-terminated cstring, no length prefix`]` only for `CHAT_MSG_CHANNEL`, then `uint64 targetGuid` unconditionally |

Then always: `uint32 len + chatText`, `uint8 chatTag`, and — only for
`CHAT_MSG_ACHIEVEMENT`/`CHAT_MSG_GUILD_ACHIEVEMENT` — a trailing
`uint32 achievementId`.

All the length-prefixed strings (`senderName`, `targetName`, `chatText`) use
the same shape: `uint32 byteLength` (includes the trailing null) followed by
that many bytes, UTF-8, null-terminated — `agent/session.py::_read_len_string`.
The `channel` name is different: a plain null-terminated cstring with no
length prefix (`agent/packets.py::cstring`).

`ObjectGuid::HighGuid` values needed to classify `targetGuid` (`Entities/Object/ObjectGuid.h`,
top 16 bits of the raw 64-bit guid): `Player = 0x0000`, `Unit = 0xF130`,
`Pet = 0xF140`, `GameObject = 0xF110`.

## The server does not echo our own position back via update-object during ordinary movement

Found live-verifying UM-38's `move_to`: after sending `MSG_MOVE_START_FORWARD`/
`MSG_MOVE_HEARTBEAT`, no `SMSG_UPDATE_OBJECT`/`SMSG_COMPRESSED_UPDATE_OBJECT`
block about our *own* guid arrives reporting the new position — `agent.
perception.WorldState.my_server_position` sat at our login/spawn position
for the entire session, confirmed by comparing it against the DB-saved
`characters.characters` position mid-walk (both matched the *start*
position, not wherever we currently were).

This makes sense once you read it as: TrinityCore does track our position
server-side (the periodic save, `characters.characters.position_*`, proves
that — see `CONTRIBUTING.md`'s "Player save interval tuning"), but a
player's own client is expected to already know where it is (it sent the
movement itself), so there's no protocol reason to echo it back via
`UPDATE_OBJECT`. Only *other* players learn our position that way (and, for
them, from our broadcast `MSG_MOVE_*` packets, not `UPDATE_OBJECT` either,
during ordinary walking — `UPDATE_OBJECT` is for state that changes
independent of movement, or the initial `CREATE`).

Consequence for `agent/movement.py`: comparing our own simulated position
against `WorldState.my_server_position` on every tick (meant to catch a
genuine server-side correction — teleport, knockback) will only ever see a
*stale* value that predates the current move, not a live "does the server
agree with us" signal. `_simulate` accounts for this: it snapshots
`my_server_position` at the start of each move and only treats a *later
change* to it as drift, never a value that was already there (or that
first becomes known — see the `CMSG_SET_ACTIVE_MOVER`-adjacent race note in
`agent/movement.py`'s comments) before the move began. In practice this
makes the drift check a dormant safety net for the rare case something
external (a real teleport/knockback) does update it — `_simulate`'s
"no measurable progress over 3 s" check is the one that actually fires
during ordinary v1 use.

## UM-39: combat wire formats

Verified against TrinityCore branch `3.3.5`:
  `src/server/game/Server/Packets/SpellPackets.cpp` (`SpellCastRequest::Read`,
    `SendSpellGo`/`SendSpellStart` payload shape, `SendCastResult`)
  `src/server/game/Spells/Spell.cpp` (`Spell::prepare`/`Spell::cast` — when
    `SendSpellStart`/`SendSpellGo` actually fire, see below)
  `src/server/game/Handlers/SpellHandler.cpp` (`SMSG_INITIAL_SPELLS`,
    `SMSG_LEARNED_SPELL`, `SMSG_SUPERCEDED_SPELL`/`SMSG_REMOVED_SPELL`)
  `src/server/game/Server/Packets/CombatPackets.cpp` (`AttackSwing`,
    `SMSG_ATTACKERSTATEUPDATE` / `HitInfo` flags)
  `src/server/shared/SharedDefines.h` (`enum SpellCastResult`, 188 values —
    `agent/spells.py::SPELL_CAST_RESULT_NAMES` maps all of them)

`agent/spells.py` holds every pure parser/builder (no opcodes, no I/O — same
split as `agent/names.py`); opcodes live in `agent/session.py`, dispatched
through `_SPELL_DISPATCH` into thirteen thin handlers that call
`WoWSession._record_event()`. `agent/actions.py` adds `auto_attack`,
`stop_attack`, `cast_spell` to the Action registry, all reading confirmation
off `session.events` (a bounded deque) rather than blocking on a single
expected reply — the same shape `set_target`/`face` established in UM-36.

`SMSG_SPELL_START`/`SMSG_SPELL_GO` are only partially parsed
(`spells.parse_spell_cast_prefix`): the fixed unconditional prefix
(caster guids, cast id, spell id, cast flags, cast time) is decoded and the
variable tail (hit/miss target lists, power data) is deliberately left
alone — packets are length-prefixed and self-delimited, so skipping a tail
doesn't desync the stream, and the agent doesn't need it yet.

**`SMSG_SPELL_START` always arrives, even for an instant cast — found live
testing `cast_spell`.** `Spell::prepare` (`Spell.cpp`) calls `SendSpellStart()`
unconditionally for a non-triggered cast, before it even checks
`m_casttime`; only *after* that, if `m_casttime == 0`, does the same call
immediately run `cast(true)` (→ `SendSpellGo()`). So a real client — and
this agent — sees `spell_start` first in all cases, then `spell_go` either
back-to-back (instant) or after `cast_time` milliseconds (the spell's own
cast time, echoed in `spell_start`'s payload).

**Bug found from this, fixed before shipping:** `CastSpellAction`'s
confirmation wait originally looked only for `spell_go`/`cast_failed`
within one fixed `confirm_timeout` (2.0 s) from when the cast was sent. Any
spell whose cast time exceeds that (Holy Light is 2.5 s) timed out on
`cast_spell` even when the cast fully succeeded, because `spell_go` simply
hadn't arrived yet. Reproduced live: casting spell 635 on self while
auto-attacking a training dummy always returned `"no cast confirmation seen
(timed out)"`, while the event log showed a matching `spell_go` arriving
~2.5 s later. Fixed in `agent/actions.py::CastSpellAction.execute` — the
wait is now two-phase: phase one waits `confirm_timeout` for `spell_start`
(near-instant) or a same-tick `cast_failed`/`spell_go`; phase two, only
once `spell_start` is seen, extends the deadline to that event's own
`cast_time` (ms) plus `confirm_timeout` as margin. Covered by
`test_execute_extends_wait_by_reported_cast_time`/
`test_execute_reports_cast_failed_after_cast_time_wait` in
`agent/tests/test_actions.py`.

`SMSG_CAST_FAILED`'s optional `failed_arg1`/`failed_arg2` fields carry no
flag bits on the wire — presence is implied purely by total payload length
(base 6 B; +4 B if ≥ 10 B; +4 B more if ≥ 14 B), so
`spells.parse_cast_failed` branches on `len(payload)` instead of a header
field.

`SMSG_ATTACKERSTATEUPDATE`'s layout is conditionally gated by its `HitInfo`
bitfield (absorb/resist/block sub-sections, an optional rage-gain field, and
an always-present trailing `unk1`/`melee_spell_id` — `spells.
parse_attacker_state_update` follows the flag checks in `Unit::
SendAttackStateUpdate`/`BuildProcResistedBlockedInfo` one for one.

Live-verified (character Luaprata, level 10 blood elf paladin, account
AGENT01): `SMSG_INITIAL_SPELLS` parsed into a 45-entry `session.spellbook`
matching the live character's real kit; `cast_spell` correctly rejects an
unknown spell id before sending anything; `auto_attack` correctly rejects
an out-of-melee-range target with an actionable "try move_towards first"
error, then, after `move_to`-ing ~280 yd to a nearby training-dummy cluster
(`Expert's Training Dummy`, world DB entry 32666 — the character's login
hub has no appropriately-leveled genuine hostile within walking range, see
below), `auto_attack` produced a confirmed `attack_start` and a stream of
real `SMSG_ATTACKERSTATEUPDATE` swings against it (0 damage — target
dummies are the expected invulnerable/no-retaliate kind); `cast_spell`
against Holy Light (635, 2.5 s cast) on self, sent mid-combat, confirmed
`ok=True` against the real `spell_go` event once the timeout fix above
landed.

**Not live-verified: killing a real hostile mob and observing
`SMSG_PARTYKILLLOG`/`SMSG_LOG_XPGAIN`.** `AGENT01`'s character (`Luaprata`)
spawns in what's clearly a custom testing/trainer hub on map 530 (class
trainers, holiday-event NPCs, and the training-dummy cluster used above) —
every creature within the reach of straight-line-only `move_to` v1 near
that hub is either a same-faction NPC or, per `world.creature_template`, a
guard/vendor that the current blunt "different faction number = hostile"
heuristic (`ObjectInfo.is_hostile_to`, documented as approximate since
UM-34) misclassifies as attackable. Confirming a real kill + XP gain needs
either a longer supervised walk into an actual leveling zone or the owner
placing/pointing at a safe low-level hostile near spawn.

## UM-41 / UM-91: quest wire formats

UM-41 reconstructed these layouts from memory. UM-91 found that most of them were
wrong: live, the quest log showed the title `"g\r"` (the zone id 3431 read as a string)
and the objective `393216: 0/1966080`. Everything below is now checked against
TrinityCore branch `3.3.5` (commit `48128f325ac5f1b597ab86b6b410d9eed1024bb1`):
`Server/Packets/QuestPackets.cpp`, `Entities/Creature/GossipDef.cpp`,
`Entities/Player/Player.cpp`, `Handlers/QuestHandler.cpp`, `Quests/QuestDef.{h,cpp}`,
`Entities/Object/Updates/UpdateFields.h`. Rows marked **live** also have a capture in
`agent/tests/fixtures/quests/` (Sunspeaker, 2026-09-24). Every parser in
`agent/quests.py` checks that it consumed exactly `len(payload)` bytes.

Constants (`QuestDef.h`): `QUEST_OBJECTIVES_COUNT 4`, `QUEST_ITEM_OBJECTIVES_COUNT 6`,
`QUEST_REWARD_CHOICES_COUNT 6`, `QUEST_REWARD_ITEM_COUNT 4`,
`QUEST_REWARD_REPUTATIONS_COUNT 5`.

### Quest log fields (`UpdateFields.h`, `Player.h`/`Player.cpp`), live

`PLAYER_QUEST_LOG_1_1 = UNIT_END + 0x0A`, 5 fields per slot (`MAX_QUEST_OFFSET`), 25 slots:

```
x_1  quest id (0 = empty slot)
x_2  state: QuestSlotStateMask bitmask. 0 = in progress, 0x1 = COMPLETE, 0x2 = FAIL.
     This is not the DB's QuestStatus: an accepted, unfinished quest is 0 on the wire
     while character_queststatus.status is 3 (QUEST_STATUS_INCOMPLETE).
x_3  counters, a uint64 over x_3/x_4: 4 x uint16, counter i = kill-credit objective i
x_5  timer
```

Only creature/GO objectives have counters (`SetQuestSlotCounter` is called from
`SendQuestUpdateAddCreatureOrGo`). Item progress is not in the update fields, so
`build_quest_log` reports item objectives with `count: None`. The perception snapshot
adds `state_name` and objective names from the creature/gameobject name cache, e.g.
`Mana Wyrm slain: 1/8`. It queues a `CMSG_CREATURE_QUERY` with guid 0 for an unknown
entry, because `HandleCreatureQueryOpcode` only uses the entry.

### `CMSG_QUEST_QUERY` (`0x05C`) → `SMSG_QUEST_QUERY_RESPONSE` (`0x05D`), live

```
CMSG_QUEST_QUERY: uint32 questId            // QueryQuestInfo::Read, nothing else
SMSG_QUEST_QUERY_RESPONSE (QueryQuestInfoResponse::Write):
  uint32 id, type, int32 level, uint32 minLevel, int32 sortId (zone), uint32 infoId, suggestedPlayers
  2 x (uint32 factionId, int32 factionValue)
  uint32 nextQuest, xpDifficulty, int32 rewardMoney, uint32 bonusMoney, displaySpell, int32 spell
  uint32 honor, float killHonor, uint32 startItem, flags, title, playerKills, talents, int32 arena, uint32 factionFlags
  4 x (uint32 item, uint32 count)          // reward items
  6 x (uint32 item, uint32 count)          // reward choice items
  5 x uint32 factionId, 5 x int32 value, 5 x int32 override
  uint32 poiMap, float poiX, float poiY, uint32 poiPriority
  cstring logTitle, logDescription (objective summary), questDescription (story), areaDescription, completionLog
  4 x (uint32 npcOrGo, uint32 count, uint32 itemDrop, uint32 itemDropCount)   // GO = entry | 0x80000000
  6 x (uint32 item, uint32 count)          // required items
  4 x cstring objectiveText                // custom label per kill objective, often ""
```

### Questgiver windows and events

```
SMSG_QUESTGIVER_STATUS        0x183: uint64 guid, uint8 status
SMSG_QUESTGIVER_QUEST_LIST    0x185: uint64 npc, cstring greeting, uint32 emoteDelay, uint32 emote, uint8 n,
                                     n x (uint32 quest, uint32 icon, int32 level, uint32 flags, uint8 repeatable, cstring title)
SMSG_QUESTGIVER_QUEST_DETAILS 0x188: uint64 npc, uint64 informUnit, uint32 quest, cstring title, details, objectives,
                                     uint8 autoLaunched, uint32 flags, suggested, uint8 startCheat,
                                     uint32 n + n x (item, count, displayId)  // choice items
                                     uint32 n + n x (item, count, displayId)  // reward items
                                     uint32 money, xp, honor, float killHonor, uint32 displaySpell, int32 spell,
                                     uint32 title, talents, arena, factionFlags, 5x3 reputation ints,
                                     int32 n + n x (uint32 type, uint32 delay)
SMSG_QUESTGIVER_REQUEST_ITEMS 0x18B: uint64 npc, int32 quest, cstring title, completionText, int32 emoteDelay, emote,
                                     autoLaunched, uint32 flags, int32 suggested, moneyToGet,
                                     uint32 n + n x (int32 item, int32 count, uint32 displayId),
                                     uint32 explored (3 = can complete), 0x04, 0x08, 0x10
SMSG_QUESTGIVER_OFFER_REWARD  0x18D: uint64 npc, uint32 quest, cstring title, rewardText, uint8 autoLaunched,
                                     uint32 flags, suggested, uint32 n + n x (delay, type),
                                     choice-item list, reward-item list (as in DETAILS), uint32 money, xp, honor,
                                     float killHonor, uint32 unused, displaySpell, int32 spell, uint32 title,
                                     talents, arena, factionFlags, 5x3 reputation ints
SMSG_QUESTGIVER_QUEST_COMPLETE 0x191: uint32 quest, xp, money, honor, talents, arena
SMSG_QUESTGIVER_QUEST_FAILED  0x192: uint32 quest, uint32 reason
SMSG_QUESTUPDATE_ADD_KILL     0x199: uint32 quest, uint32 npcOrGo, uint32 count, uint32 required, uint64 guid   (live)
SMSG_QUESTUPDATE_ADD_ITEM     0x19A: empty on 3.3.5 (the writes are commented out in Player.cpp)
SMSG_QUESTUPDATE_COMPLETE     0x198: uint32 quest
```

The window item lists only contain non-empty slots (`Quest::BuildQuestRewards`). A kill
quest with no required items skips REQUEST_ITEMS and goes straight to OFFER_REWARD
(`PlayerMenu::SendQuestGiverRequestItems`).

Client requests (`QuestHandler.cpp` read order):

```
CMSG_QUESTGIVER_STATUS_QUERY   0x182: uint64 guid
CMSG_QUESTGIVER_HELLO          0x184: uint64 guid
CMSG_QUESTGIVER_QUERY_QUEST    0x186: uint64 guid, uint32 quest, uint8 respondToGiver
CMSG_QUESTGIVER_ACCEPT_QUEST   0x189: uint64 guid, uint32 quest, uint32 startCheat
CMSG_QUESTGIVER_COMPLETE_QUEST 0x18A: uint64 guid, uint32 quest
CMSG_QUESTGIVER_REQUEST_REWARD 0x18C: uint64 guid, uint32 quest
CMSG_QUESTGIVER_CHOOSE_REWARD  0x18E: uint64 guid, uint32 quest, uint32 rewardIndex
CMSG_QUESTLOG_REMOVE_QUEST     0x194: uint8 slot
```

**Known gap:** `build_questgiver_query_quest` omits the trailing `uint8 respondToGiver`,
so the server drops the packet as too short. `accept_quest` still works because
ACCEPT_QUEST doesn't depend on it; this was verified live when 8325 was accepted from a
gossip window. The builder is left unchanged here because fixing it would make the
server open a quest-details window in the middle of `accept_quest`. That change belongs
in `agent/actions.py`.

Quest events land in `session.events`: `quest_progress` (`objective: "kill"|"item"`),
`quest_complete`, `quest_turned_in`, `quest_failed`. Before UM-91, `quest_progress`
passed `kind="kill"` next to `_record_event`'s own `kind` argument, which raised
`TypeError`, so every ADD_KILL was dropped.

## UM-59: player trade wire formats

Verified against TrinityCore branch `3.3.5`:
  `src/server/game/Handlers/TradeHandler.cpp` (every CMSG_* handler,
    `WorldSession::SendTradeStatus`, `WorldSession::SendUpdateTrade`)
  `src/server/game/Server/Packets/TradePackets.h` (`CancelTrade::Read` — empty)
  `src/server/game/Entities/Player/TradeData.h`/`.cpp` (`TradeSlots` enum,
    `SetItem`/`SetMoney`/`SetAccepted` — which status goes to which side)
  `src/server/game/Entities/Player/Player.cpp` (`Player::TradeCancel`)
  `src/server/game/Entities/Object/ObjectDefines.h` (`TRADE_DISTANCE = 11.11f`)
  `src/server/shared/SharedDefines.h` (`enum TradeStatus`)
  `src/server/game/Entities/Item/ItemTemplate.h` (`ITEM_FIELD_FLAG_SOULBOUND`,
    `MAX_ITEM_PROTO_SOCKETS = 3`)

Opcodes (`Opcodes.h`): `CMSG_INITIATE_TRADE 0x116`, `CMSG_BEGIN_TRADE 0x117`,
`CMSG_BUSY_TRADE 0x118`, `CMSG_IGNORE_TRADE 0x119`, `CMSG_ACCEPT_TRADE 0x11A`,
`CMSG_UNACCEPT_TRADE 0x11B`, `CMSG_CANCEL_TRADE 0x11C`,
`CMSG_SET_TRADE_ITEM 0x11D`, `CMSG_CLEAR_TRADE_ITEM 0x11E`,
`CMSG_SET_TRADE_GOLD 0x11F`, `SMSG_TRADE_STATUS 0x120`,
`SMSG_TRADE_STATUS_EXTENDED 0x121`. Every `ObjectGuid` in this section is a
**raw** 8-byte read/write (`ObjectGuid.cpp`'s plain `operator<</operator>>`)
— not the variable-length `PackedGuid` movement/update-object use, which is
a distinct type only used explicitly in this branch.

**The single most important trap: our own offer is never echoed back to
us.** `TradeData::SetItem`/`SetMoney` only ever call `Update(forTrader=true)`
— which sends `SMSG_TRADE_STATUS_EXTENDED` to *the trade partner*, telling
them about *our* new offer. Nothing equivalent goes back to the player who
just changed their own offer (a real client already updated its own window
optimistically the moment it sent the packet). So `agent/perception.py`'s
`world.trade["my_items"]`/`"my_gold"` are tracked client-side the moment
`agent/actions.py` sends `CMSG_SET_TRADE_ITEM`/`CMSG_SET_TRADE_GOLD` — only
`their_items`/`their_gold` ever arrives from the server. What the sender
*does* reliably get back is `SMSG_TRADE_STATUS`: `TRADE_STATUS_BACK_TO_TRADE`
(7) on success — `TradeData::SetAccepted(false)` is unconditional, so *any*
offer change on *either* side un-accepts both sides and answers both
players — or a rejection (`TRADE_STATUS_TRADE_CANCELED` for a bad
bag/slot/already-offered item, `TRADE_STATUS_NOT_ON_TAPLIST` for a soulbound
one, `TRADE_STATUS_CLOSE_WINDOW` for `CMSG_SET_TRADE_GOLD` with insufficient
funds). `agent/actions.py`'s `offer_item`/`offer_gold` wait for one of
those, keyed off a raw `"trade_status"` event `agent/session.py` records for
every `SMSG_TRADE_STATUS` (not just the named `trade_requested`/
`trade_completed`/`trade_cancelled`/`trade_offer_rejected` events
`WorldState.apply_trade_status` decides on top of it).

**Accepting first gets no reply at all — this is the expected, common
case, not a stall.** `HandleAcceptTradeOpcode` only answers the *other*
player (`TRADE_STATUS_TRADE_ACCEPT`) when the partner hasn't accepted yet;
the accepting player themselves gets nothing until the trade actually
completes or fails. `AcceptTradeAction.execute()` sends the packet, marks
its own `my_accepted` optimistically, waits briefly for
`trade_completed`/`trade_cancelled`, and reports `ok=True` either way if
neither arrives — unlike every other action in this codebase, a timeout
here isn't a failure.

`SMSG_TRADE_STATUS` (`WorldSession::SendTradeStatus`): `uint32 status`
always, then a status-specific tail selected by a `switch` — everything not
listed below has no tail at all:
```
status == TRADE_STATUS_BEGIN_TRADE (1):    uint64 traderGuid (raw)         // the guid of whoever initiated
status == TRADE_STATUS_OPEN_WINDOW (2):    uint32 (always 0, unused)
status == TRADE_STATUS_CLOSE_WINDOW (12):  uint32 result (InventoryResult), uint8 isTargetResult, uint32 itemLimitCategoryId
status in (WRONG_REALM 22, NOT_ON_TAPLIST 23): uint8 slot
```

`SMSG_TRADE_STATUS_EXTENDED` (`WorldSession::SendUpdateTrade`): `uint8
traderData` (1 = this describes the *other* side's offer — the only value
this agent ever receives in practice, per the trap above), `uint32 tradeId`
(always 0), `uint32 x2` slot counts (always `TRADE_SLOT_COUNT` = 7),
`uint32 money`, `uint32 spell` (enchant spell cast on slot 6, out of scope
here), then exactly `TRADE_SLOT_COUNT` (7) fixed entries — `TRADE_SLOT_TRADED_COUNT`
(6) real trade slots (0-5) plus slot 6 (`TRADE_SLOT_NONTRADED`, the
enchant-reagent slot, not modeled): `uint8 slotIndex`, then either a real
item's 17 fields or 18 zero `uint32`s for an empty slot (`entry == 0` means
empty — no separate presence flag on the wire):
```
uint32 entry, displayId, stackCount, wrapped(0/1)
uint64 giftCreatorGuid (raw)
uint32 permEnchantId, socketEnchant[3]   // MAX_ITEM_PROTO_SOCKETS = 3
uint64 creatorGuid (raw)
uint32 charges, suffixFactor, randomPropertyId, lockId, maxDurability, durability
```

Outgoing payloads: `CMSG_INITIATE_TRADE` = raw `uint64` target guid;
`CMSG_BEGIN_TRADE`/`BUSY_TRADE`/`IGNORE_TRADE`/`ACCEPT_TRADE`/
`UNACCEPT_TRADE`/`CANCEL_TRADE` are all empty; `CMSG_SET_TRADE_ITEM` =
`uint8 tradeSlot, uint8 bag, uint8 slot`; `CMSG_CLEAR_TRADE_ITEM` = `uint8
tradeSlot`; `CMSG_SET_TRADE_GOLD` = `uint32 copper`.

Live-verified: byte-level fixtures captured with `AGENT_DUMP_PACKETS` from
a real two-agent trade on the LAN realm (2026-09-17, Farstrider/AGENT02 +
Shadowblade/AGENT03 — see `agent/tests/fixtures/trade/README.md`).
`agent/tests/test_trade.py` covers every parser/builder with hand-built
bytes and re-parses the live captures in `RealFixtureIntegrationTest`.

## UM-60: mailbox wire formats

Verified against TrinityCore branch `3.3.5`:
  `src/server/game/Handlers/MailHandler.cpp` (every CMSG_* handler,
    `WorldSession::CanOpenMailBox` — the mailbox-guid/range check every mail
    opcode shares; sending `CMSG_GET_MAIL_LIST` *is* "opening the mailbox",
    there's no separate use/hello opcode the way NPC windows have one)
  `src/server/game/Server/Packets/MailPackets.h`/`.cpp` — despite the
    "3.3.5" branch name, this handler was retrofitted onto the modern
    typed-packet framework (`WorldPackets::Mail::*`); verified as the
    byte-for-byte format this branch's build actually sends/expects, same
    situation as `CancelTrade` in `TradeHandler.cpp` (see UM-59's section
    above) — confirmed live (below), not just by reading source.
  `src/server/game/Server/Packets/PacketUtilities.h` (`String<N, ...>`
    reads via the same `ReadCString` as a plain `std::string` — still a
    plain null-terminated cstring on the wire, not length-prefixed)
  `src/server/game/Mails/Mail.h` (`MAX_MAIL_ITEMS`, `MailMessageType`,
    `MailCheckMask`)
  `src/server/shared/SharedDefines.h` (`enum MailResponseType`,
    `enum MailResponseResult`, `GAMEOBJECT_TYPE_MAILBOX = 19`)
  `src/server/game/Entities/Unit/UnitDefines.h` (`UNIT_NPC_FLAG_MAILBOX`)
  `src/server/game/Entities/Item/ItemDefines.h` (`MAX_INSPECTED_ENCHANTMENT_SLOT`)

Opcodes: `CMSG_SEND_MAIL 0x238` → `SMSG_SEND_MAIL_RESULT 0x239`,
`CMSG_GET_MAIL_LIST 0x23A` → `SMSG_MAIL_LIST_RESULT 0x23B`,
`CMSG_MAIL_TAKE_MONEY 0x245`, `CMSG_MAIL_TAKE_ITEM 0x246`,
`CMSG_MAIL_MARK_AS_READ 0x247`, `CMSG_MAIL_DELETE 0x249`,
`SMSG_RECEIVED_MAIL 0x285`. Every GUID is raw 8 bytes, same as trade/npc.

**Finding a mailbox needs a gameobject's queried `type`, and that
resolution has a caching trap — found live testing.** A mailbox is
usually a gameobject; `ObjectInfo.is_mailbox()` checks its queried
`SMSG_GAMEOBJECT_QUERY_RESPONSE` `type == GAMEOBJECT_TYPE_MAILBOX` (19).
`agent.names.NameCache` persists creature/gameobject templates to disk
across process runs (static data, safe to cache) — but
`WorldState._maybe_resolve_name`'s *cached* branch for gameobjects only
ever backfilled `.name` onto the freshly-perceived object, never `.type`,
so on a second run against an already-warm cache `is_mailbox()` stayed
`False` forever even though the correct type was sitting right there in
the cached dict. Reproduced live (Luaprata, Silvermoon City mailbox,
entry `182363`): first run resolved fine (fresh query), second run
against the warm cache did not, until the cached branch was fixed to
backfill `gameobject_type` too.

**Sending mail costs postage even with no gold or item attached — found
live testing, not obvious from a skim of the handler.** `HandleSendMail`'s
`cost = !Attachments.empty() ? 30 * Attachments.size() : 30` always
charges at least 30 copper (`MAIL_POSTAGE_COPPER`), added to whatever
gold is being sent, checked as one `reqmoney` total. A level-1 character
with 0 copper cannot send *any* mail, not even a text-only letter — this
blocked a full live send/receive round trip this session (no agent
character had 30 copper); confirmed instead by sending the real
`CMSG_SEND_MAIL` and capturing the server's own
`SMSG_SEND_MAIL_RESULT`/`MAIL_ERR_NOT_ENOUGH_MONEY` reply (fixture:
`agent/tests/fixtures/mail/send_mail_result_not_enough_money.bin`) —
proves the request's byte layout is accepted and parsed correctly by the
real server, just not affordable.

`SMSG_SEND_MAIL_RESULT` (`MailCommandResult::Write`) answers **four**
different client opcodes through one shared reply, disambiguated by its
`command` field (`MailResponseType`): `MAIL_SEND` (0, from `send_mail`),
`MAIL_MONEY_TAKEN` (1) and `MAIL_ITEM_TAKEN` (2, both halves of
`take_mail`), `MAIL_DELETED` (4, from `delete_mail`) — `agent/session.py`
records one raw `"mail_result"` event per reply regardless of which,
and `agent/actions.py`'s four mail actions each filter by
`command`/`mail_id`/(for items) `attach_id` to find the reply that's
theirs, the same "raw event + action-side filter" shape UM-59's
`"trade_status"` event uses.

An item attachment's `AttachID` (used later in `CMSG_MAIL_TAKE_ITEM`) is
**not** a small positional index — `HandleMailTakeItem` compares it
against the mailed item's own GUID low part (`MailAttachedItem::AttachID
= item->GetGUID().GetCounter()`), so it must be echoed back verbatim from
the mail list entry, never recomputed.

Live-verified (Luaprata, level 10 blood elf paladin, account AGENT01,
2026-09-17): walked ~182 yd with `move_to` (one call, arrived exactly)
from her login position to the nearest known mailbox (Silvermoon City,
gameobject entry `182363`); `is_mailbox()` correctly recognized it once
`gameobject_type` resolved (see the caching trap above); `open_mailbox()`
sent a real `CMSG_GET_MAIL_LIST` and received a real, correctly-parsed
`SMSG_MAIL_LIST_RESULT` (empty inbox, `total_records=0`) — fixture:
`agent/tests/fixtures/mail/mail_list_result_empty.bin`; a raw
`CMSG_SEND_MAIL` (bypassing `send_mail`'s own client-side gold check, to
see the server's real answer) got back a real, correctly-parsed
`SMSG_SEND_MAIL_RESULT` with `MAIL_ERR_NOT_ENOUGH_MONEY` as described
above.

**Not live-verified:** a successful send (`MAIL_OK`), any inbox with real
mail in it (attachments, COD, a non-`MAIL_NORMAL` sender), `take_mail`,
`delete_mail`, and `SMSG_RECEIVED_MAIL` — all blocked this session by no
agent character having any gold. `agent/tests/test_mail.py`'s hand-built-
byte tests cover every one of those layouts; only the live round trip
wasn't exercised for them. See the UM-60 PR for what's left as a human
step.

**Gold, not movement, is the actual blocker — confirmed live.** Tried to
earn the 30-copper postage honestly (no GM commands): sold Farstrider's
only tradeable item (a starting-tier shield) to a nearby vendor
(Shara Sunwing, Sunstrider Isle) for 3 copper — nowhere near enough.
Separately, since every agent character's spawn is far from a mailbox,
tested whether `move_to` can even cover that distance at all: Farstrider
walked the full ~968 yd from Sunstrider Isle to the same Silvermoon City
mailbox Luaprata used, in **one** `move_to` call, arriving within 0.17 yd
in 136 s, no obstacles, no "stuck". So v1's straight-line movement *can*
reach a mailbox from every agent's spawn — the only reason a full live
send/receive round trip hasn't happened yet is that no agent has enough
gold, not a movement/pathing limitation. Farstrider was left logged out
right at the Silvermoon mailbox (3 copper) — a future session with
~27 more copper on hand (or on any character that can reach him/a
mailbox) can finish this test immediately without any more walking.

builder with hand-built bytes instead.
