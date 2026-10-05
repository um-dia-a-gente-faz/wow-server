# ADR 0005: Split WoWSession into transport, state and routed domain handlers

## Status

Accepted. Records a decision already merged in PR #256 (commit `e620b75`).

## Context

`agent/session.py` had grown to about 1670 lines (per the commit message of
`e620b75`): socket framing and RC4, every piece of game state, login, the recv
loop and the packet handlers for quests, loot, trade, mail, NPCs, spells,
death, chat and world updates all lived in the one `WoWSession` class. Any
protocol change touched the same file, and a handler could reach anything on
the session.

## Decision

`WoWSession` is now a thin class that inherits two others and delegates the rest
(`agent/session.py`: `class WoWSession(Transport, GameState)`):

| Module | Owns |
|---|---|
| `agent/transport.py` (`Transport`) | the socket, RC4 framing, `_send_packet` / `_recv_packet` / `_dump_packet`, the send lock, the per-opcode handler-error throttle. No game state. |
| `agent/state.py` (`GameState`) | per-character state: identity, position, level/xp, `world_state`, `chat_inbox`, `events`, spellbook, loot, corpse/death fields, the reconnect flags. |
| `agent/router.py` | `PacketRouter` and `Context`, see ADR 0006. |
| `agent/handlers/*.py` | one module per domain (`chat`, `death`, `loot`, `mail`, `npc`, `quests`, `spells`, `trade`, `world`); each registers its opcodes on the shared router at import time. |
| `agent/session.py` | the login flow, the recv loop, keepalive, `logout`, `join_channels`, and the four connection-lifecycle handlers (time sync, pong, stand state, logout complete). |

The packet builders and parsers (`agent/npc.py`, `quests.py`, `loot.py`, ...)
were not moved; they stay pure functions and the handlers call them.

## Consequences

- `session.py` is now about 400 lines. A new packet type is a new handler plus a
  `register` call, not an edit to the session class.
- Handlers receive a `Context`, not the session, so what they may touch is
  explicit (ADR 0006).
- `WoWSession` is still one object that actions, reflexes, the HTTP observer and
  the think loop all take as `sess`; the split is internal. Callers did not change.
- Importing `agent.session` is what registers the handlers (it imports every
  handler module). A module that builds a router context without importing it gets
  an empty table.
- `session.py` still defines the login-time opcode constants itself. PR #247
  (centralise opcodes) is in flight and is expected to change that; this ADR does
  not describe it as done.
