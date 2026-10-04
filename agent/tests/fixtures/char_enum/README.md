# SMSG_CHAR_ENUM fixtures

Real, live-captured `SMSG_CHAR_ENUM` (`0x03B`) payloads from `192.168.1.64`.

Each file is the raw application payload — the bytes `WoWSession.enum_characters()`
sees after framing is stripped, starting with the character-count byte — not a
whole packet. Feed one through a `FakeSocket` with `server_packet()` as
`agent/tests/test_session.py::CharEnumRealPayloadTest` does.

## Files

| File | Account | Characters | Bytes |
|---|---|---|---|
| `three_characters.bin` | `AGENT01` | 3 — `Luaprata` (guid 2), `Dawnrunner` (7), `Jevrun` (8), all level 1 Blood Elf Paladins on map 530 | 835 |

Captured 2026-10-04 while staging the Jev brain (#70, stage 1), after `Jevrun`
was created on `AGENT01` — the first agent account in the fleet to hold more
than one character.

## Why this fixture exists (gh-199)

The bug only shows from the **second** record on, so an account with exactly one
character (which was every agent account until this run) parsed fine. On this
payload the parser returned `['Luaprata', '']` plus a garbage entry, and
`python3 -m agent --dry-run` with `WOW_CHARACTER=Jevrun` logged into `Luaprata`
instead.

The per-character tail after the character flags is 224 bytes, as TrinityCore
3.3.5's `Player::BuildEnumData()` writes it (`Player.cpp`, branch `3.3.5`):

| field | bytes |
|---|---|
| `customizeFlags` (uint32, **always** present) | 4 |
| `firstLogin` (uint8) | 1 |
| pet displayId, level, family (3 x uint32) | 12 |
| 23 x (`INVENTORY_SLOT_BAG_END`) equipment slot: uint32 displayId, uint8 inventoryType, uint32 enchantVisual | 207 |

The corrected parser consumes this payload to exactly `len(payload)`.
