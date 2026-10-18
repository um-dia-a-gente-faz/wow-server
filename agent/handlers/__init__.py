"""Per-domain packet handlers. Each module exposes `register(router)`; the
modules and their order are listed once, in `build_router()` (ADR 0006)."""

from ..metrics import count_router_event
from ..router import PacketRouter
from . import chat, death, group, loot, mail, npc, quests, spells, trade, world, world_states

# Registration order is the tick order (names, npc text, items, quests) and the
# order of PacketRouter._handlers. Changing it changes behaviour; a test pins it.
MODULES = (death, world, world_states, chat, group, mail, npc, spells, trade, loot, quests)


def build_router() -> PacketRouter:
    """A fresh router with every domain handler registered, in MODULES order."""
    router = PacketRouter(count=count_router_event)   # the real metrics sink (#494)
    for module in MODULES:
        module.register(router)
    return router
