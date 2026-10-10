"""World state: a live, thread-safe model of what the agent perceives (#265).

Split by domain: `objects` (ObjectInfo + flag constants), `world` (WorldState
and its mixins `resolution`, `windows`, `trade_state`), and one pure builder per
snapshot section (`inventory`, `quest_log`, `nearby`) composed by `snapshot`.
Callers keep importing from `agent.perception`.
"""

from .objects import (  # noqa: F401
    GAMEOBJECT_TYPE_MAILBOX,
    PLAYER_FLAGS_GHOST,
    POWER_MANA,
    UNIT_DYNFLAG_LOOTABLE,
    UNIT_NPC_FLAG_GOSSIP,
    UNIT_NPC_FLAG_MAILBOX,
    UNIT_NPC_FLAG_QUESTGIVER,
    UNIT_NPC_FLAG_SPIRITGUIDE,
    UNIT_NPC_FLAG_SPIRITHEALER,
    UNIT_NPC_FLAG_TRAINER,
    UNIT_NPC_FLAG_VENDOR,
    ObjectInfo,
    PerceptionParseError,
)
from .world import WorldState  # noqa: F401
