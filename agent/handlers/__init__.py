"""Per-domain packet handlers. Each module exposes `register(router)`; the
modules and their order are listed once, in `build_router()` (ADR 0006)."""

from ..router import PacketRouter
from . import chat, death, group, loot, mail, npc, quests, spells, trade, world

# Registration order is the tick order (names, npc text, items, quests) and the
# order of PacketRouter._handlers. Changing it changes behaviour; a test pins it.
MODULES = (death, world, chat, group, mail, npc, spells, trade, loot, quests)


def build_router() -> PacketRouter:
    """A fresh router with every domain handler registered, in MODULES order."""
    router = PacketRouter()
    for module in MODULES:
        module.register(router)
    return router
