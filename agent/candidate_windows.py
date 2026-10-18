"""Candidates for an open NPC window (trainer, vendor, quest and gossip windows), split out of
candidates.py as a pure move. `window_candidates` is the only provider-side caller; candidates.py
re-exports every name here, so `candidates.candidate`, `_sellable` and `_backpack` keep working.

Nothing here imports agent.candidates: the caps are read from agent.action_specs, the same
values candidates.py pins as MAX_TRAIN, MAX_SELL and MAX_GOSSIP_OPTIONS.
"""

from . import action_names as A, item_compare
from .action_specs import cap_of
from .missions import DEFAULT_MISSION
from .model import Candidate, Item, Snapshot

_BACKPACK_FIRST, _BACKPACK_LAST = 23, 38


def candidate(action: str, params: dict, label: str) -> Candidate:
    """Build one candidate with its stable id."""
    key = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return {"id": f"{action}:{key}" if key else action, "label": label,
            "action": action, "params": dict(params)}


def _backpack(item: Item) -> bool:
    return isinstance(item.get("slot"), int) and _BACKPACK_FIRST <= item["slot"] <= _BACKPACK_LAST


def _sellable(item: Item, mission: str) -> bool:
    return _backpack(item) and item_compare.sellable(item, mission)


def _window_candidates(snapshot: Snapshot, mission: str = DEFAULT_MISSION) -> list[Candidate]:
    window = snapshot.get("window")
    if not isinstance(window, dict):
        return []
    kind = window.get("kind")
    # A vendor window names its NPC vendor_guid (npc.parse_list_inventory via apply_list_inventory), not npc_guid.
    npc_guid = window.get("npc_guid", window.get("vendor_guid"))
    in_log = {q.get("quest_id") for q in snapshot.get("quest_log") or []}
    out = []

    if kind == "trainer" and npc_guid is not None:
        known = {s.get("id") for s in snapshot.get("spells") or []}
        for spell in (window.get("spells") or [])[:cap_of(A.TRAIN_SPELL)]:
            if spell.get("spell_id") not in known:
                out.append(candidate(A.TRAIN_SPELL, {"trainer_guid": npc_guid, "spell_id": spell["spell_id"]},
                                     f"train spell {spell['spell_id']}"))
    if kind == "vendor" and npc_guid is not None:
        inventory = snapshot.get("inventory") or []
        for item in inventory:
            if _sellable(item, mission):
                grey = (item.get("template") or {}).get("quality") == item_compare.ITEM_QUALITY_POOR
                out.append(candidate(A.SELL_ITEM, {"vendor_guid": npc_guid, "bag": 255, "slot": item["slot"]},
                                     f"sell {'grey' if grey else 'gold'} item {item.get('name') or item.get('entry')}"))
                if sum(c["action"] == A.SELL_ITEM for c in out) >= cap_of(A.SELL_ITEM):
                    break

    if kind == "quest_details" and npc_guid is not None:
        out.append(candidate(A.ACCEPT_QUEST, {"npc_guid": npc_guid, "quest_id": window["quest_id"]},
                             f"accept quest {window.get('title') or window['quest_id']}"))
    elif kind in ("quest_list", "gossip") and npc_guid is not None:
        for q in window.get("quests") or []:
            title = q.get("title") or q["quest_id"]
            if q["quest_id"] in in_log:
                out.append(candidate(A.COMPLETE_QUEST, {"npc_guid": npc_guid, "quest_id": q["quest_id"]},
                                     f"hand in quest {title}"))
            else:
                out.append(candidate(A.ACCEPT_QUEST, {"npc_guid": npc_guid, "quest_id": q["quest_id"]},
                                     f"accept quest {title}"))
        if kind == "gossip":
            # Coded options need typed text; that's free text, so never offered.
            options = [o for o in window.get("options") or [] if not o.get("coded")]
            for o in options[:cap_of(A.GOSSIP_SELECT)]:
                out.append(candidate(A.GOSSIP_SELECT, {"option_index": o["index"]},
                                     f"choose dialogue option: {o.get('text') or o['index']}"))
    elif kind == "quest_request_items" and npc_guid is not None:
        out.append(candidate(A.COMPLETE_QUEST, {"npc_guid": npc_guid, "quest_id": window["quest_id"]},
                             f"hand in quest {window.get('title') or window['quest_id']}"))
    elif kind == "quest_offer_reward" and npc_guid is not None:
        title = window.get("title") or window["quest_id"]
        choices = window.get("reward_choice_items") or []
        for i, item in enumerate(choices or [None]):
            what = f" taking reward item {item.get('entry')}" if item else ""
            out.append(candidate(A.TURN_IN_QUEST,
                                 {"npc_guid": npc_guid, "quest_id": window["quest_id"], "reward_choice": i},
                                 f"turn in quest {title}{what}"))

    out.append(candidate(A.CLOSE_WINDOW, {}, f"close the {kind or 'open'} window"))
    return out
