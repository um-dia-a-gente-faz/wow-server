---
name: trinity-protocol
description: Get WoW 3.3.5a (build 12340) wire formats right when writing or reviewing agent protocol code — how to verify opcodes, packet layouts and update fields against TrinityCore source, and the parsing traps this project has already been burned by. Use when adding or changing anything that builds or parses a packet, or when reviewing such a change.
---

# 3.3.5a protocol work

`docs/PROTOCOL-NOTES.md` holds the layouts already verified for this project. Add to it
rather than re-deriving. **`docs/NEXT-AGENT-HANDOFF.md`'s old layout table is wrong.**

## Verify, never remember

For every opcode, field index and constant you touch, fetch the real source from the
`3.3.5` branch and cite `path` (and ideally the commit) in a code comment:

```bash
curl -s https://raw.githubusercontent.com/TrinityCore/TrinityCore/3.3.5/src/server/game/Server/Protocol/Opcodes.h | grep -n "CMSG_LOOT\b"
curl -s https://raw.githubusercontent.com/TrinityCore/TrinityCore/3.3.5/src/server/game/Handlers/LootHandler.cpp | sed -n '1,80p'
gh api "repos/TrinityCore/TrinityCore/git/trees/3.3.5?recursive=1" --jq '.tree[].path' | grep -i mailhandler
```

Rules of thumb:
- The **handler's read order** is the wire order. A packet class's *member declaration*
  order can differ — that bug shipped once (`CTextEmote` reads `EmoteID, SoundIndex,
  Target`, while the header lists `Target` first).
- Some packets are hand-built in `Player.cpp`/`Object.cpp` rather than a packet class;
  search both before concluding a layout.
- Field indices come from the auto-generated `UpdateFields.h` for 12340. Copy them,
  don't compute them from another expansion's table.

## Traps already hit here

- **Packed GUIDs are little-endian**: bit *i* of the mask means byte *i*. Use
  `packets.unpack_packed_guid`, never a hand-rolled shift loop.
- **VALUES_UPDATE writes all mask words first, then all values.** Not interleaved.
- **Fields worth 0 are not sent at all**, so absent ≠ zero. Decode into something that
  distinguishes "unknown" from 0, and merge per field instead of replacing a whole list
  (a compacted list loses which power type each number is).
- **Large server packets use a 5-byte header** when the size's top bit is set; mis-parsing
  it kills the recv thread.
- **Spline movement** (`MOVEMENTFLAG_SPLINE_ENABLED`) appears in ordinary bursts; an
  unparsed conditional path costs the whole packet, not one object.
- **Never report success before the server answers.** An action that only sends a packet
  and returns `ok=True` will lie: a purchase with no money "succeeded" until the buy
  reply was parsed. Wait for the ack/failure event, or say the result is unconfirmed.
- Chat: `LANG_UNIVERSAL` is rejected as a hacking attempt even for GMs; send the racial
  language. NPC/monster chat uses a different branch from player chat.

## Testing protocol code

- Fixtures first: real payloads live in `agent/tests/fixtures/`; capture more with
  `AGENT_DUMP_PACKETS=<dir>`. A parser test should assert it consumed exactly
  `len(payload)` bytes.
- Golden-byte tests for anything you build; hand-build bytes for conditional paths the
  fixtures don't happen to contain.
- Then prove it live (see the `live-agent-test` skill) and paste the log into the PR.
