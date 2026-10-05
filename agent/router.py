"""Table-driven packet routing.

Domain modules under agent/handlers register `opcode -> handler(ctx, payload)`
on the shared ROUTER at import time; WoWSession only calls ROUTER.dispatch().
Unknown opcodes return False. Handlers get a Context (game state + send/dump),
not the whole session."""

from typing import Callable


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
    def __init__(self):
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
        handler = self._handlers.get(opcode)
        if handler is None:
            return False
        handler(ctx, payload)
        return True

    def tick(self, ctx: Context):
        for fn in self._ticks:
            fn(ctx)


ROUTER = PacketRouter()
