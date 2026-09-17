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
