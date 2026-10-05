# ADR 0006: Table-driven PacketRouter with a restricted handler Context

## Status

Accepted. Records a decision already merged in PR #256 (commit `e620b75`).

## Context

Dispatch used to be a long `if opcode == ...` chain inside the session class,
with each branch free to read and write any session attribute. Adding an opcode
meant editing that chain, and nothing stopped two branches handling the same
opcode.

## Decision

`agent/router.py` holds one process-wide `ROUTER = PacketRouter()`:

- `register(opcode, handler)` and `register_all({opcode: handler})`. Registering an
  opcode twice raises `ValueError`.
- `dispatch(ctx, opcode, payload)` runs `handler(ctx, payload)` and returns `True`,
  or returns `False` when no handler is registered.
- `on_tick(fn)` registers `fn(ctx)` for `ROUTER.tick(ctx)`, which the recv loop
  calls once per iteration (after every packet, and after every 0.5 s read
  timeout). The handler modules use it to send pending queries (names, NPC text,
  items, quests). Tick order is registration order, which is import order in
  `session.py`.
- A handler gets a `Context` (`state` = the `GameState`, plus `send(opcode, payload)`
  and `dump(opcode, data)`), not the session. `send` and `dump` look up
  `_send_packet` / `_dump_packet` on the transport per call, so a test that
  replaces `_send_packet` is honoured.

`WoWSession._dispatch_guarded` wraps `ROUTER.dispatch`: an exception in a handler
increments `dropped_packets`, logs at most once per opcode per 30 s
(`ERROR_LOG_INTERVAL_S`) and drops only that packet; the connection stays up.

## Consequences

- The table is global, not per session. Handlers must keep per-character data on
  `ctx.state`, never in module globals, or two sessions in one process would share it.
  (The agent runs one session per process today; tests are the only place two exist.)
- `Context` is a convention, not a sandbox: `ctx.state` is the full `GameState`.
- A raised handler error is swallowed after logging, so a bug shows up as a
  rising `dropped_packets` and a rate-limited warning rather than a disconnect.
- Tests cover the router in `agent/tests/test_router.py`.
- PR #247 (centralise opcodes) is in flight; the registration keys are plain
  opcode integers until it lands.
