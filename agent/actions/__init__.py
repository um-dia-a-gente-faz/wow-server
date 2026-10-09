"""Game actions for the agent, split by domain (issue #248).

`base` owns Action/ActionResult/REGISTRY/register/catalog and the `send`
session facade; each other module registers its actions on import. The import
order below is the registration order, which is the order of catalog() (what
the LLM sees), so keep it. Everything the old single-file agent/actions.py
exposed is re-exported here, so `from agent import actions` is unchanged.
"""

from .base import *  # noqa: F401,F403
from .base import _wait_for, _wait_for_value, _pause_follow_reflex, _find_item_guid  # noqa: F401
from .movement import *  # noqa: F401,F403
from .combat import *  # noqa: F401,F403
from .vendor import *  # noqa: F401,F403
from .vendor import _find_vendor_item  # noqa: F401
from .chat import *  # noqa: F401,F403
from .chat import _encode_message, _racial_language, _send_chat, _known_player_name, _normalise_chat  # noqa: F401
from .loot import *  # noqa: F401,F403
from .loot import _resolve_item_template  # noqa: F401
from .quest import *  # noqa: F401,F403
from .quest import _open_quest_giver_window  # noqa: F401
from .trade import *  # noqa: F401,F403
from .trade import _find_trade_status_event, _find_event_since, _resolve_offered_item  # noqa: F401
from .mail import *  # noqa: F401,F403
from .mail import _find_nearby_mailbox, _mail_item_flags  # noqa: F401
from .. import loot as lootmod, item_compare  # noqa: F401  (tests reach these through the package)
