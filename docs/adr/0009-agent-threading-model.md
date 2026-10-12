# ADR 0009: Agent threading model as it is today

## Status

Descriptive, not a design. It records what the code does on `main`, verified by
reading it. Issue #251 was narrowed on 2026-10-12; the section "Decision of #251"
records what changed and what stays open. Open questions are collected at the end.

## Context

One agent process drives one character. After #256 the pieces are in separate
modules, which makes the threads easy to see and the sharing rules easy to get
wrong. This ADR writes the current rules down so a change can be judged against them.

## Threads

| Thread | Started in | Does |
|---|---|---|
| main / think loop | `agent/runloop.py::run_think_loop` | perceive, one brain decision and action per cycle (`think_and_act`), then sleeps to the cycle deadline (`AGENT_THINK_INTERVAL_S`) |
| recv thread | `WoWSession.login_character` (`agent/session.py`), daemon | `_recv_loop`: read a packet, `router.dispatch` via `_dispatch_guarded`, `router.tick`; sends `CMSG_KEEP_ALIVE` after 15 s of silence; on a dead socket sets `unexpected_disconnect` |
| follow and rest reflexes | `run_loop` in `runloop.py`, two daemon threads | tick `FollowReflex` / `RestReflex` every 0.3 s on `sess.world_state`; exceptions are logged and swallowed per tick |
| observer HTTP | `agent/http_api.py::start_server`, daemon `ThreadingHTTPServer` (one thread per request), only when `AGENT_HTTP_PORT` is set | read-only GETs; `POST /control/walk` only with `AGENT_CONTROL_TOKEN` |
| movement worker | `movement.Mover._run` | a short-lived thread that simulates a walk; the caller `join()`s it, so a move is synchronous for its caller |
| chat relay | `agent/chat_relay.py`, daemon `chat-relay` | posts heard chat to chat-feed from a queue |

The reconnect supervisor is the main thread: it creates a new session per attempt
and tears the old one down. It is called by `Agent.run()` (`agent/root.py`).

## Who writes, who reads

- The recv thread is the writer of the packet-derived state: handlers mutate
  `GameState` and `WorldState` through the `Context`.
- It is not the only writer of `GameState`. `movement.py` assigns
  `session.player_position` from the mover thread while a walk runs;
  `agent/actions/` also assigns `player_position`, `pending_invite` and
  `channel_say_history`; `record_event` is called from `death.py` and
  `agent/actions/`. Those run on the think thread, a reflex thread or the control path,
  depending on the caller.
- Reflexes read `world_state` and send packets through `_send_packet` and
  `actions.send_*`; the follow reflex also calls actions that move the character.
- The think loop, the reflexes and the observer read.

## Synchronisation that exists

- `Transport._lock` serialises writes to the socket, so any thread may send.
- `WorldState` has one internal lock; its docstring says mutation goes through
  `update_object()` / `remove_guids()` from the recv thread and that `snapshot()`
  and the getters are safe from other threads. Caveat (#701): `snapshot()` is not
  consistent under a concurrent writer. It composes the sections from live objects
  after its lock is released; see "Decision of #251" below.
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
`agent/actions/` waits for confirmation by scanning `events` for entries newer than a
`time.monotonic()` stamp (`record_event` stamps `t` for that reason).

Readers on any thread but the recv thread take a snapshot, `GameState.events_snapshot()`
or `chat_inbox_snapshot()`, and never loop over the deque itself (#692): CPython raises
`RuntimeError: deque mutated during iteration` when an append lands mid-loop, which
happens exactly while an action waits for an event, and nothing on the think path catches
it, so it ends the agent process. `list(deque)` is safe because the copy runs in C
without releasing the GIL, so no append can interleave with it. That holds for the GIL
builds of CPython (3.12 here); a free-threaded build would need a lock around the deque.
A fitness test in `agent/tests/test_architecture.py` rejects a live loop outside
`agent/state.py` and `agent/handlers/`.

## Consequences

- A new handler may assume it runs on the recv thread, serially, so handlers do
  not need locks against each other.
- Anything a handler stores that another thread reads needs either `WorldState`'s
  lock or a copy-on-read.
- A reflex and the think loop can both act at once: the `action_lock` covers the
  think loop against operator walks only.

## Decision of #251 (2026-10-12)

Issue #251 asked for an event bus, explicit state ownership and race tests. Most of
it was delivered by other issues since it was written: the session references no
audit, metrics or relay code (the observers are built in `agent/root.py`, #292), and
sends go through the router (ADR 0006). The decision narrows #251 to the following.

- **No event bus while there is one subscriber.** The only consumer of game events
  outside the handlers is the chat relay (`GameState.chat_relay`, read by
  `agent/handlers/chat.py::relay_chat`). A publish/subscribe layer for one subscriber
  is not built. It is revisited with the activity-event contract (#390), the likely
  second consumer, which should bring its own design.
- **Dead observer removed.** `GameState.on_update_object` had no setter and no caller
  and is deleted.
- **Open question 3 is answered**, in three parts:
  - Stale reads of scalar `GameState` fields are accepted. They are plain attribute
    assignments, and readers tolerate a slightly old value.
  - Iterating the unlocked deques (`events`, `chat_inbox`) from another thread is not
    accepted. Such readers go through `events_snapshot()` / `chat_inbox_snapshot()`
    (#692), pinned by `agent/tests/test_events_snapshot.py`; `test_architecture.py`
    rejects a live loop outside `agent/state.py` and `agent/handlers/`.
  - `WorldState` getters read under its lock. `WorldState.snapshot()` is NOT yet
    consistent under a concurrent writer: it copies the object table under its lock,
    then composes the sections from the live `ObjectInfo` objects after releasing the
    lock, so a writer can change an object between two of its field reads. This is
    tracked as #701, which owns the race test and the fix. Until #701 lands, the
    statement that `snapshot()` is safe from other threads holds only for its
    lock-held part.
- **Open questions 1 and 2 stay open.** They ask for a single writer for `GameState`
  and for reflex and think-loop coordination. Both change the threading model.
  Nothing measured so far needs that change, so it is not started. Reopen them when a
  measured problem asks for it.

## Open questions

Questions 1 and 2 are open (see the Decision of #251 above). Question 3 was answered
there. Question 4 was resolved in #371.

1. Should `GameState` have a single writer (the recv thread), with the mover and
   actions posting changes to it instead of assigning fields?
2. Do the reflexes need to coordinate with the think loop (e.g. both issuing
   movement), given `action_lock` does not cover them?
3. (Answered 2026-10-12, see "Decision of #251".)
4. (Resolved in #371: the router is per session, ADR 0006.) Does any plan to run several sessions in
   one process need it per session?
