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
