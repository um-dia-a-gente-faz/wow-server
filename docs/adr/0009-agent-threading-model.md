# ADR 0009: Agent threading model as it is today

## Status

Descriptive, not a design. It records what the code does on `main`, verified by
reading it. Issue #251 may change the model; nothing here proposes how.
Open questions are collected at the end.

## Context

One agent process drives one character. After #256 the pieces are in separate
modules, which makes the threads easy to see and the sharing rules easy to get
wrong. This ADR writes the current rules down so a change can be judged against them.

## Threads

| Thread | Started in | Does |
|---|---|---|
| main / think loop | `agent/__main__.py::_run_think_loop` | perceive, one brain decision and action per cycle (`think_and_act`), then sleeps to the cycle deadline (`AGENT_THINK_INTERVAL_S`) |
| recv thread | `WoWSession.login_character` (`agent/session.py`), daemon | `_recv_loop`: read a packet, `ROUTER.dispatch` via `_dispatch_guarded`, `ROUTER.tick`; sends `CMSG_KEEP_ALIVE` after 15 s of silence; on a dead socket sets `unexpected_disconnect` |
| follow and rest reflexes | `_run_loop` in `__main__.py`, two daemon threads | tick `FollowReflex` / `RestReflex` every 0.3 s on `sess.world_state`; exceptions are logged and swallowed per tick |
| observer HTTP | `agent/http_api.py::start_server`, daemon `ThreadingHTTPServer` (one thread per request), only when `AGENT_HTTP_PORT` is set | read-only GETs; `POST /control/walk` only with `AGENT_CONTROL_TOKEN` |
| movement worker | `movement.Mover._run` | a short-lived thread that simulates a walk; the caller `join()`s it, so a move is synchronous for its caller |
| chat relay | `agent/chat_relay.py`, daemon `chat-relay` | posts heard chat to chat-feed from a queue |

The reconnect supervisor is the main thread: it creates a new session per attempt
and tears the old one down.

## Who writes, who reads

- The recv thread is the writer of the packet-derived state: handlers mutate
  `GameState` and `WorldState` through the `Context`.
- It is not the only writer of `GameState`. `movement.py` assigns
  `session.player_position` from the mover thread while a walk runs;
  `actions.py` also assigns `player_position`, `pending_invite` and
  `channel_say_history`; `record_event` is called from `death.py` and
  `actions.py`. Those run on the think thread, a reflex thread or the control path,
  depending on the caller.
- Reflexes read `world_state` and send packets through `_send_packet` and
  `actions.send_*`; the follow reflex also calls actions that move the character.
- The think loop, the reflexes and the observer read.

## Synchronisation that exists

- `Transport._lock` serialises writes to the socket, so any thread may send.
- `WorldState` has one internal lock; its docstring says mutation goes through
  `update_object()` / `remove_guids()` from the recv thread and that `snapshot()`
  and the getters are safe from other threads.
- `HandleMap` (`agent/handles.py`) and `AgentObserver` (`_lock` for sequence and
  token totals) have their own small locks.
- `AgentObserver.action_lock` (#178): held by the think loop for the whole of each
  cycle's `think_and_act`, and by an operator walk for its whole duration, so the
  two never drive the character at once. The reflex threads do not take it.
- `Mover` has one active movement per session; starting a new one stops the old.
- `http_api.py` documents the rule for the observer: read through the locked
  getters or copy a container first, never hold a lock the game loop holds for
  long, and the game loop never waits on a handler.

## Not synchronised

`GameState` scalar fields (`player_position`, `level`, `xp`, `coinage`,
`pending_invite`, ...) and the `events` / `chat_inbox` deques have no lock. They
are plain attribute assignments and deque appends; the code relies on that being
safe enough under CPython's GIL and on readers tolerating a slightly stale value.
`actions.py` waits for confirmation by scanning `events` for entries newer than a
`time.monotonic()` stamp (`record_event` stamps `t` for that reason).

## Consequences

- A new handler may assume it runs on the recv thread, serially, so handlers do
  not need locks against each other.
- Anything a handler stores that another thread reads needs either `WorldState`'s
  lock or a copy-on-read.
- A reflex and the think loop can both act at once: the `action_lock` covers the
  think loop against operator walks only.

## Open questions

These are not answered here and may be the subject of #251.

1. Should `GameState` have a single writer (the recv thread), with the mover and
   actions posting changes to it instead of assigning fields?
2. Do the reflexes need to coordinate with the think loop (e.g. both issuing
   movement), given `action_lock` does not cover them?
3. Is relying on the GIL for unlocked fields and deques acceptable, or should reads
   go through a snapshot like `WorldState.snapshot()`?
4. `ROUTER` is process-global (ADR 0006); does any plan to run several sessions in
   one process need it per session?
