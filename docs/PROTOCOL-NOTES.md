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
