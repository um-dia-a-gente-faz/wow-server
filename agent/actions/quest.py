"""Quest actions: accept, complete, turn in, abandon (UM-41).

Split out of the former agent/actions.py (issue #248).
"""

from .. import quests as qu
from ..rules import INTERACT_RANGE_YD
from .base import (Action, ActionResult, register, send)


# ── Quests (UM-41) ─────────────────────────────────────────────────────────
# Wire layouts + citations/caveats live in agent/quests.py — none of this
# module's byte layouts have been live-verified against a real server (see
# the module docstring there); everything here is still wrapped by
# session.py's usual per-packet try/except-and-drop.



def _open_quest_giver_window(session, world, npc_guid: int, quest_id: int, kinds: tuple):
    window = world.get_ui_state()
    if window is None or window.get("kind") not in kinds:
        return None
    if window.get("npc_guid") != npc_guid:
        return None
    if window.get("quest_id") is not None and window.get("quest_id") != quest_id:
        return None
    return window


@register
class AcceptQuestAction(Action):
    name = "accept_quest"
    description = ("Accept a quest offered by a nearby questgiver. Requires that NPC's quest "
                    "list/quest details window, or a gossip window listing the quest, to "
                    "already be open (interact() with the NPC first) and the quest to be "
                    "present in it.")
    params = {
        "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
        "quest_id": {"type": "integer", "description": "Quest ID to accept, from the open "
                                                         "quest_list/quest_details/gossip "
                                                         "window."},
    }
    required = ("npc_guid", "quest_id")

    def check(self, session, world, npc_guid: int, quest_id: int, **_) -> str | None:
        target = world.get_object(npc_guid)
        if target is None:
            return f"guid {npc_guid:#x} is not currently perceived"
        if session.player_position is not None:
            distance = target.distance_to(session.player_position)
            if distance is not None and distance > INTERACT_RANGE_YD:
                return (f"guid {npc_guid:#x} is {distance:.1f} yd away, out of interact range "
                        f"({INTERACT_RANGE_YD} yd) — try move_towards first")
        window = world.get_ui_state()
        if window is None or window.get("kind") not in ("quest_list", "quest_details", "gossip"):
            return "no quest list/details/gossip window is open for that NPC — interact() with it first"
        if window.get("npc_guid") != npc_guid:
            return "the open quest window belongs to a different NPC"
        if window.get("kind") in ("quest_list", "gossip"):
            # A gossip-primary questgiver (npc_flags with both gossip and
            # questgiver set, e.g. Magistrix Erona) opens a "gossip" window
            # that already lists its offered quests (agent.npc.
            # parse_gossip_message's `quests`), never a quest_list/
            # quest_details window — accept_quest must work against that
            # window's `quests` the same way it does for quest_list's.
            if not any(q["quest_id"] == quest_id for q in window.get("quests", [])):
                return f"quest {quest_id} is not offered in the open quest window"
        elif window.get("quest_id") != quest_id:
            return "the open quest details window is for a different quest"
        return None

    def execute(self, session, world, npc_guid: int, quest_id: int, **_) -> ActionResult:
        window = world.get_ui_state()
        if window is not None and window.get("kind") == "gossip":
            # The gossip window only carries the quest's id/title/level, not
            # the full quest details the server expects the client to have
            # queried before accepting it — bridge that with
            # CMSG_QUESTGIVER_QUERY_QUEST (build_questgiver_query_quest) so
            # accept_quest works directly from a gossip window without the
            # LLM needing to know about the quest_list/gossip distinction.
            send(session, qu.CMSG_QUESTGIVER_QUERY_QUEST,
                                  qu.build_questgiver_query_quest(npc_guid, quest_id))
        send(session, qu.CMSG_QUESTGIVER_ACCEPT_QUEST,
                              qu.build_questgiver_accept_quest(npc_guid, quest_id))
        return ActionResult(ok=True, detail={"npc_guid": npc_guid, "quest_id": quest_id})


@register
class CompleteQuestAction(Action):
    name = "complete_quest"
    description = ("Tell a nearby questgiver you're ready to turn in quest_id — the first step "
                    "of turning a quest in. Opens the request-items or offer-reward window "
                    "(check the perception snapshot's `window` after calling this); use "
                    "turn_in_quest to actually finish once that window is open.")
    params = {
        "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
        "quest_id": {"type": "integer", "description": "Quest ID to complete, from quest_log."},
    }
    required = ("npc_guid", "quest_id")

    def check(self, session, world, npc_guid: int, quest_id: int, **_) -> str | None:
        target = world.get_object(npc_guid)
        if target is None:
            return f"guid {npc_guid:#x} is not currently perceived"
        if session.player_position is not None:
            distance = target.distance_to(session.player_position)
            if distance is not None and distance > INTERACT_RANGE_YD:
                return (f"guid {npc_guid:#x} is {distance:.1f} yd away, out of interact range "
                        f"({INTERACT_RANGE_YD} yd) — try move_towards first")
        quest_log = world.build_quest_log()
        if not any(q["quest_id"] == quest_id for q in quest_log):
            return f"quest {quest_id} is not in the quest log"
        return None

    def execute(self, session, world, npc_guid: int, quest_id: int, **_) -> ActionResult:
        send(session, qu.CMSG_QUESTGIVER_COMPLETE_QUEST,
                              qu.build_questgiver_complete_quest(npc_guid, quest_id))
        return ActionResult(ok=True, detail={"npc_guid": npc_guid, "quest_id": quest_id})


@register
class TurnInQuestAction(Action):
    name = "turn_in_quest"
    description = ("Finish turning in quest_id at a nearby questgiver, picking reward_choice "
                    "(index into the open offer-reward window's reward_choice_items, if any). "
                    "Requires the offer-reward window to already be open (call complete_quest "
                    "first).")
    params = {
        "npc_guid": {"type": "string", "description": "Snapshot handle (a string like \"u3\") of the questgiver NPC."},
        "quest_id": {"type": "integer", "description": "Quest ID to turn in."},
        "reward_choice": {"type": "integer", "description": "Index of the reward item to pick, "
                                                              "from the open window's "
                                                              "reward_choice_items. Default 0."},
    }
    required = ("npc_guid", "quest_id")

    def check(self, session, world, npc_guid: int, quest_id: int, reward_choice: int = 0,
              **_) -> str | None:
        window = _open_quest_giver_window(session, world, npc_guid, quest_id, ("quest_offer_reward",))
        if window is None:
            return ("no offer-reward window is open for that NPC/quest — call complete_quest "
                     "first")
        choices = window.get("reward_choice_items", [])
        if choices and not (0 <= reward_choice < len(choices)):
            return f"reward_choice {reward_choice} is out of range (0..{len(choices) - 1})"
        return None

    def execute(self, session, world, npc_guid: int, quest_id: int, reward_choice: int = 0,
                **_) -> ActionResult:
        send(session, qu.CMSG_QUESTGIVER_CHOOSE_REWARD,
                              qu.build_questgiver_choose_reward(npc_guid, quest_id, reward_choice))
        return ActionResult(ok=True, detail={"npc_guid": npc_guid, "quest_id": quest_id,
                                              "reward_choice": reward_choice})


@register
class AbandonQuestAction(Action):
    name = "abandon_quest"
    description = "Drop a quest from the quest log by its slot number. Irreversible."
    params = {
        "slot": {"type": "integer", "description": "Quest log slot (0-24), from quest_log's "
                                                     "`slot` field — not the quest id."},
    }
    required = ("slot",)

    def check(self, session, world, slot: int, **_) -> str | None:
        quest_log = world.build_quest_log()
        if not any(q["slot"] == slot for q in quest_log):
            return f"quest log slot {slot} is empty"
        return None

    def execute(self, session, world, slot: int, **_) -> ActionResult:
        send(session, qu.CMSG_QUESTLOG_REMOVE_QUEST, qu.build_questlog_remove_quest(slot))
        return ActionResult(ok=True, detail={"slot": slot})
