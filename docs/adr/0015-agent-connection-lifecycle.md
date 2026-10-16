# ADR 0015: Agent connection lifecycle as a state machine

## Status

Proposed (#584). The owner accepts it later. The module is `agent/lifecycle.py`; since #292
the reconnect supervisor (`agent/supervisor.py`) drives it, and each transition is written to
the audit directory and shown by `GET /state`.

## Context

`supervise_connection` in `agent/supervisor.py` (in `agent/__main__.py` when this ADR was written) runs the connection lifecycle through
control flow: `build_session()` is one state, `run_session()` another, the backoff sleep a
third, and the stop path a fourth. The states are implicit. Nothing can ask "what state is
the connection in" or "may it go from here to there", and the observer cannot show the
supervisor's history without reading its log.

This ADR proposes making the states and the allowed moves explicit in one pure module, so
the transition table can be tested on its own and read by the HTTP observer later.

## States

| State | Meaning |
|---|---|
| CONNECTING | `build_session()` is running: auth, realm, world login; also the one start-delay wait before the first attempt (#539) |
| ONLINE | In world, `run_session()` is running |
| BACKOFF | Waiting before the next connect attempt |
| STOPPING | Stop requested, the run ended or the supervisor is unwinding. The session is logged out if there is one: there is none on `BACKOFF > STOPPING` and `CONNECTING > STOPPING` |
| STOPPED | Terminal. The supervisor returned or was unwound by an exception; the reason says which (`stop`, `run_ended`, `crashed`) |
| FAILED | Terminal, permanent refusal (`AuthRejected` with a permanent code) |

## Transitions

Each row is one allowed `(From, To)` pair, the table `TRANSITIONS` in `agent/lifecycle.py`.
Any other pair is invalid and changes nothing.

| From | To | When |
|---|---|---|
| CONNECTING | ONLINE | connected |
| CONNECTING | BACKOFF | connect failed or transient auth refusal |
| CONNECTING | FAILED | permanent auth refusal |
| CONNECTING | STOPPING | the supervisor is unwound by an exception that `build_session()` let through (no stop check runs before connecting today), or a stop is requested during the start-delay wait before the first attempt (#539) |
| ONLINE | BACKOFF | disconnected |
| ONLINE | STOPPING | stop requested, the run ended, or `run_session()` raised |
| BACKOFF | CONNECTING | backoff elapsed |
| BACKOFF | STOPPING | stop requested, or the wait raised |
| STOPPING | STOPPED | teardown done |

`STOPPED` and `FAILED` have no outgoing transitions. Every non-terminal state can reach
`STOPPED`.

## Reasons

`to()` takes any text as the reason. The supervisor passes one of the reasons below, the set
`REASONS` in `agent/lifecycle.py`.

| Reason | Transitions | When |
|---|---|---|
| `connected` | CONNECTING > ONLINE | `build_session()` returned a session |
| `connect_failed` | CONNECTING > BACKOFF | `build_session()` raised anything but `AuthRejected` |
| `auth_rejected` | CONNECTING > BACKOFF | `AuthRejected` with a transient code |
| `auth_rejected_permanent` | CONNECTING > FAILED | `AuthRejected` with a permanent code |
| `disconnected` | ONLINE > BACKOFF | `run_session()` returned true and no stop was requested |
| `stream_desync` | ONLINE > BACKOFF | as `disconnected`, and the world stream was declared desynced (#793: a declared size above 1 MiB, or 16 unknown opcodes in a row); the session's `desync_reason` says which |
| `backoff_elapsed` | BACKOFF > CONNECTING | the wait ended and no stop was requested |
| `stop` | CONNECTING > STOPPING, ONLINE > STOPPING, BACKOFF > STOPPING, STOPPING > STOPPED | the stop event is set (SIGTERM, SIGINT) |
| `run_ended` | ONLINE > STOPPING, STOPPING > STOPPED | `run_session()` returned false and no stop was requested (the duration elapsed) |
| `crashed` | CONNECTING > STOPPING, ONLINE > STOPPING, BACKOFF > STOPPING, STOPPING > STOPPED | an exception is leaving the supervisor and the machine is not terminal |

## Decisions from the review of #587

Settled in #292 before the supervisor was wired.

1. **A crash ends in `STOPPED` with the reason `crashed`.** There is no `ONLINE > FAILED` row and
   `FAILED` keeps its one meaning, a permanent refusal. An exception can leave the supervisor from
   `CONNECTING`, `ONLINE` and `BACKOFF` (the injected `sleep` may raise), and all three already reach
   `STOPPING` and then `STOPPED`, so one unwind rule covers them with no new row. `STOPPED` is
   therefore not always clean: a reader that needs to know checks the reason.
2. **`CONNECTING > STOPPING` stays, and no stop check is added before connecting.** The row is what
   the unwind rule uses from `CONNECTING`. A stop check before `build_session()` would change what
   the agent does on SIGTERM, and that is a separate decision. The start-delay wait (#539) checks
   `stop` after it, and nothing else does.
3. **One writer.** Only the thread that runs the supervisor calls `to()`, and the callback never
   calls `to()`. Callbacks then run in transition order, so records written from the callback are
   in order. Other threads read. A signal handler never touches the machine: the lock is not
   re-entrant.
4. **`STOPPING` does not imply a session.** The States table says so.
5. **The dry run has no lifecycle.** `--dry-run` does not go through the supervisor and has no audit
   log or observer; it keeps its own connect, sleep and logout.
6. **Where the lines go.** Lifecycle lines go to their own file
   `<audit_dir>/<agent>/lifecycle/<YYYY-MM-DD>.jsonl` and never to the decision day file.
7. **`GET /state`.** It carries an optional `lifecycle` object; `api_version` stays 1.
8. **The clock.** The production machine uses the wall clock (`time.time`), so `at`, `since` and
   the line's `ts` are epoch seconds.
9. **The reason of `STOPPING > STOPPED`.** It is the reason of the `STOPPING` before it, except
   that when an exception leaves the supervisor while the machine is already in `STOPPING` (the
   logout raised) it is `crashed`. `ONLINE > STOPPING` `stop` then `STOPPING > STOPPED` `crashed`
   is valid.

## Out of scope

- Player state (alive, dead, ghost). That is game state owned by `agent/death.py`. It is
  orthogonal to the connection and stays there.
- The composition root and the supervisor wiring. Done in #292.
- Exposure in the audit log and `/api/state`. Done in #292.

## Consequences

- The table is one place to read and test. The test suite checks every ordered pair of
  states, the supervisor's walk, the history cap and the JSON shape of the snapshot.
- Transitions are serialised by one lock. The `on_transition` callback runs after the lock
  is released, so it may read the state. An exception from the callback propagates, but the
  state change has already happened.
- The machine records what happened and when, using an injected clock. It does not log and
  does not emit metrics. The owner decides what to do with each transition.
- A supervisor that adopts it must map each of its control-flow exits to one of the nine
  rows above. A new path needs a new row here first.
