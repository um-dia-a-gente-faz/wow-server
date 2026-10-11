# ADR 0015: Agent connection lifecycle as a state machine

## Status

Proposed (#584). The owner accepts it later. This ADR records a proposal: the module
`agent/lifecycle.py` exists, nothing uses it, and the supervisor does not use it yet.
Wiring it in is #292.

## Context

`_supervise_connection` in `agent/__main__.py` runs the connection lifecycle through
control flow: `build_session()` is one state, `run_session()` another, the backoff sleep a
third, and the stop path a fourth. The states are implicit. Nothing can ask "what state is
the connection in" or "may it go from here to there", and the observer cannot show the
supervisor's history without reading its log.

This ADR proposes making the states and the allowed moves explicit in one pure module, so
the transition table can be tested on its own and read by the HTTP observer later.

## States

| State | Meaning |
|---|---|
| CONNECTING | `build_session()` is running: auth, realm, world login |
| ONLINE | In world, `run_session()` is running |
| BACKOFF | Waiting before the next connect attempt |
| STOPPING | Stop requested or the run ended; tearing down the session |
| STOPPED | Terminal, clean |
| FAILED | Terminal, permanent refusal (`AuthRejected` with a permanent code) |

## Transitions

Each row is one allowed `(From, To)` pair, the table `TRANSITIONS` in `agent/lifecycle.py`.
Any other pair is invalid and changes nothing.

| From | To | When |
|---|---|---|
| CONNECTING | ONLINE | connected |
| CONNECTING | BACKOFF | connect failed or transient auth refusal |
| CONNECTING | FAILED | permanent auth refusal |
| CONNECTING | STOPPING | stop requested |
| ONLINE | BACKOFF | disconnected |
| ONLINE | STOPPING | stop requested |
| BACKOFF | CONNECTING | backoff elapsed |
| BACKOFF | STOPPING | stop requested |
| STOPPING | STOPPED | teardown done |

`STOPPED` and `FAILED` have no outgoing transitions. Every non-terminal state can reach
`STOPPED`.

## Out of scope

- Player state (alive, dead, ghost). That is game state owned by `agent/death.py`. It is
  orthogonal to the connection and stays there.
- The composition root and the supervisor wiring. That is #292, which is blocked by this
  proposal.
- Exposure in the audit log and `/api/state`. That is #292 too.

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
