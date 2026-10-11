"""Table-driven packet routing.

Domain modules under agent/handlers expose `register(router)`; build_router()
in agent/handlers lists them and returns a fresh PacketRouter, which a WoWSession
owns and calls .dispatch() on.
Unknown opcodes return False. Handlers get a Context (game state + send/dump),
not the whole session."""

from collections.abc import Callable

from .wire import PacketTruncated


class Context:
    """What a handler may touch: the game state, sending a packet, and the
    optional packet dump. `state` is the session's GameState; the transport
    stays behind `send`/`dump` (looked up per call so a replaced
    `_send_packet`, e.g. in tests, is honoured)."""

    __slots__ = ("state", "_transport")

    def __init__(self, state, transport):
        self.state = state
        self._transport = transport

    def send(self, opcode: int, payload: bytes = b''):
        self._transport._send_packet(opcode, payload)

    def dump(self, opcode: int, data: bytes):
        self._transport._dump_packet(opcode, data)


class PacketRouter:
    def __init__(self, count: Callable[[str, int], None] = lambda kind, opcode: None):
        """`count(kind, opcode)` is the metrics sink (kinds: packets, unhandled, truncated,
        handler_errors); handlers.build_router wires the real one, the default is a no-op."""
        self._count = count
        self._handlers: dict[int, Callable] = {}
        self._ticks: list[Callable] = []

    def register(self, opcode: int, handler: Callable):
        if opcode in self._handlers:
            raise ValueError(f"opcode {opcode:#05x} already registered")
        self._handlers[opcode] = handler

    def register_all(self, table: dict):
        for opcode, handler in table.items():
            self.register(opcode, handler)

    def on_tick(self, fn: Callable):
        """Register fn(ctx), run once per recv-loop iteration (pending queries)."""
        self._ticks.append(fn)

    def dispatch(self, ctx: Context, opcode: int, payload: bytes) -> bool:
        """Run the handler for `opcode`. False if nobody handles it."""
        count = self._count
        count("packets", opcode)
        handler = self._handlers.get(opcode)
        if handler is None:
            count("unhandled", opcode)
            return False
        try:
            handler(ctx, payload)
        except PacketTruncated:
            count("truncated", opcode)
            raise
        except Exception:
            count("handler_errors", opcode)
            raise
        return True

    def tick(self, ctx: Context):
        for fn in self._ticks:
            fn(ctx)

