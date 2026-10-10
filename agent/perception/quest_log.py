"""The `quest_log` section of the snapshot (UM-41)."""

from .. import quests as qu
from .. import update_fields as uf
from ..model import QuestEntry


def build_quest_log(me, quest_texts, names) -> list[QuestEntry]:
    """UM-41: the self player's quest log (agent.update_fields.
    decode_quest_log) enriched with cached quest text/rewards (title,
    objectives) and a best-effort human-readable progress string per
    objective, e.g. "Mana Wyrm slain: 3/8" — built from the quest log's
    raw counters against the cached quest's required_credit/
    required_items counts (agent.quests.QuestCache), when known.
    Queues a CMSG_QUEST_QUERY for any quest id seen that isn't cached
    yet (drained by session.py like the name/npc-text caches).
    Caller holds the WorldState lock."""
    if me is None:
        return []
    out: list[QuestEntry] = []
    for slot in uf.decode_quest_log(me.raw_fields):
        quest_id = slot["quest_id"]
        cached = quest_texts.quests.get(quest_id)
        if cached is None:
            quest_texts.want(quest_id)
        entry: QuestEntry = {
            "slot": slot["slot"], "quest_id": quest_id, "state": slot["state"],
            "state_name": qu.quest_slot_state_name(slot["state"]),
            "counters": slot["counters"], "time": slot["time"],
        }
        if cached is not None:
            entry["title"] = cached.get("title")
            entry["objectives_text"] = cached.get("objectives")
            entry["objectives"] = _quest_objectives_progress(slot["counters"], cached, names)
        out.append(entry)
    return out


def _quest_objectives_progress(counters: list, cached: dict, names=None) -> list:
    """Per-objective progress for one quest-log entry, e.g. {"name": "Mana
    Wyrm", "count": 3, "needed": 8, "text": "Mana Wyrm slain: 3/8"}.

    Quest-log counter i belongs to kill-credit objective i
    (Player::SendQuestUpdateAddCreatureOrGo / SetQuestSlotCounter), so the
    4 creature/GO slots of the cached SMSG_QUEST_QUERY_RESPONSE pair with
    counters by index. Item objectives have no counter in the update fields
    (the server tracks them from the bags), so their `count` is None.
    Names come from the creature/gameobject name cache (`names`, an
    agent.names.NameCache); an unknown entry is queued for a query and
    shown by id until the answer arrives. Caller holds the WorldState lock.
    """
    out = []
    for i, req in enumerate(cached.get("required_credit") or []):
        if not req.get("entry") or not req.get("count"):
            continue
        is_go = bool(req.get("gameobject"))
        name = _template_name(names, req["entry"], is_go)
        label = req.get("text") or (f"{name} slain" if not is_go else name)
        count = counters[i] if i < len(counters) else 0
        out.append({"entry": req["entry"], "gameobject": is_go, "name": name,
                    "count": count, "needed": req["count"],
                    "text": f"{label}: {count}/{req['count']}"})
    for req in cached.get("required_items") or []:
        if not req.get("entry") or not req.get("count"):
            continue
        out.append({"item": req["entry"], "count": None, "needed": req["count"],
                    "text": f"item {req['entry']}: ?/{req['count']}"})
    return out


def _template_name(names, entry: int, is_go: bool) -> str:
    kind = "gameobject" if is_go else "creature"
    if names is not None:
        cache = names.gameobjects if is_go else names.creatures
        data = cache.get(entry)
        if data and data.get("name"):
            return data["name"]
        if entry not in cache:
            # CMSG_CREATURE_QUERY/CMSG_GAMEOBJECT_QUERY: the handler only
            # uses the entry, so no sample guid is needed.
            getattr(names, "want_" + kind)(entry, 0)
    return f"{kind} {entry}"


