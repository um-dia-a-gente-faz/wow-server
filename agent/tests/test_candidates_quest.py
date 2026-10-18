"""#315: objective-driven quest candidates (kill and gameobject objectives) and the
candidate sequence of a whole quest: find the giver, accept, kill, turn in."""

import unittest

from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent.reflexes import follow  # noqa: F401  registers follow/assist/stop_following
from agent.reflexes import rest  # noqa: F401

GIVER = 5001
QUEST = 8330


def kill(entry, count, needed, name="Mana Wyrm"):
    return {"entry": entry, "gameobject": False, "name": name, "count": count, "needed": needed,
            "text": f"{name} slain: {count}/{needed}"}


def quest(objectives, state="incomplete", quest_id=QUEST, title="Mana Wyrms"):
    return {"slot": 0, "quest_id": quest_id, "state": 0, "state_name": state, "counters": [0, 0, 0, 0],
            "time": 0, "title": title, "objectives": objectives}


def mob(guid, entry, distance, **kw):
    return {"guid": guid, "entry": entry, "name": f"mob{guid}", "type": "unit", "distance": distance,
            "level": 3, "health_pct": 1.0, "in_combat": False, "npc_flags": 0, **kw}


def snap(units=(), objects=(), quest_log=(), window=None):
    return {"me": {"level": 3, "health": "100/100"}, "position": {"map": 1, "x": 0, "y": 0, "z": 0},
            "is_dead": False, "is_ghost": False, "nearby_units": list(units), "nearby_players": [],
            "nearby_objects": list(objects), "quest_log": list(quest_log), "window": window,
            "inventory": [], "equipment": {}, "spells": []}


def ids(cands):
    return [c["id"] for c in cands]


def quest_reasons(cands):
    return [c for c in cands if "quest:" in c["label"]]


class KillObjectiveTest(unittest.TestCase):
    def test_matching_mobs_are_offered_before_a_plain_attack(self):
        s = snap(units=[mob(61, 1001, 8), mob(62, 1001, 9), mob(63, 2002, 4)],
                 quest_log=[quest([kill(1001, 0, 3)])])
        cands = cand.generate(s, my_guid=1)
        self.assertEqual(ids(cands)[:3], ["move_towards:guid=61,stop_distance=3.0",
                                          "move_towards:guid=62,stop_distance=3.0",
                                          "auto_attack:guid=63"])
        self.assertIn("quest: Mana Wyrm slain: 0/3", cands[0]["label"])
        self.assertNotIn("quest:", cands[2]["label"])

    def test_objective_already_complete_offers_no_quest_target(self):
        s = snap(units=[mob(61, 1001, 4)], quest_log=[quest([kill(1001, 3, 3)])])
        cands = cand.generate(s, my_guid=1)
        self.assertEqual(quest_reasons(cands), [])
        self.assertEqual(ids(cands)[0], "auto_attack:guid=61")

    def test_matching_elite_is_still_not_pulled(self):
        s = snap(units=[mob(61, 1001, 4, rank="elite")], quest_log=[quest([kill(1001, 0, 3)])])
        cands = cand.generate(s, my_guid=1)
        self.assertNotIn("guid=61", " ".join(ids(cands)))

    def test_matching_mob_already_attacking_us_is_defence_not_quest(self):
        s = snap(units=[mob(61, 1001, 4, in_combat=True, target_guid=1)],
                 quest_log=[quest([kill(1001, 0, 3)])])
        cands = cand.generate(s, my_guid=1)
        self.assertEqual(ids(cands)[0], "auto_attack:guid=61")
        self.assertIn("attacking you", cands[0]["label"])
        self.assertNotIn("quest:", cands[0]["label"])

    def test_quest_targets_capped_at_the_attack_cap(self):
        s = snap(units=[mob(60 + i, 1001, 6 + i) for i in range(6)], quest_log=[quest([kill(1001, 0, 9)])])
        self.assertEqual(len(quest_reasons(cand.generate(s, my_guid=1))), cand.MAX_ATTACK)

    def test_quest_mobs_at_the_attack_cap_do_not_crowd_out_a_plain_attack(self):
        # Three matching mobs fill MAX_ATTACK; the plain attack on the fourth (non-matching) mob must
        # still be offered, and each quest mob appears once (the offered-guid skip in _attack_provider).
        quest_mobs = [mob(61, 1001, 6), mob(62, 1001, 7), mob(63, 1001, 8)]
        plain = mob(64, 2002, 20)
        cands = cand.generate(snap(units=quest_mobs + [plain], quest_log=[quest([kill(1001, 0, 3)])]), my_guid=1)
        for guid in (61, 62, 63):
            with self.subTest(guid=guid):
                self.assertEqual(sum(c["params"].get("guid") == guid for c in cands), 1)
        self.assertIn("move_towards:guid=64,stop_distance=3.0", ids(cands))
        self.assertEqual(len(quest_reasons(cands)), cand.MAX_ATTACK)

    def test_failed_quest_offers_no_target(self):
        s = snap(units=[mob(61, 1001, 4)], quest_log=[quest([kill(1001, 0, 3)], state="failed")])
        self.assertEqual(quest_reasons(cand.generate(s, my_guid=1)), [])

    def test_handle_guids_dedupe_without_a_second_plain_attack(self):
        class Handles:
            def resolve(self, h):
                return {"u61": 61, "u62": 62, "u63": 63}[h]

        s = snap(units=[mob("u61", 1001, 8), mob("u62", 2002, 4)], quest_log=[quest([kill(1001, 0, 3)])])
        cands = cand.generate(s, my_guid=1, handles=Handles())
        self.assertEqual(ids(cands)[:2], ["move_towards:guid=u61,stop_distance=3.0", "auto_attack:guid=u62"])
        self.assertEqual(len([c for c in cands if "u61" in c["id"]]), 1)


class GameobjectObjectiveTest(unittest.TestCase):
    def go(self, guid, entry, distance):
        return {"guid": guid, "entry": entry, "name": f"chest{guid}", "type": "gameobject",
                "distance": distance}

    def test_gameobject_in_reach_is_interacted_with(self):
        obj = self.go(70, 3333, 3.0)
        cands = cand.generate(snap(objects=[obj], quest_log=[quest([{
            "entry": 3333, "gameobject": True, "name": "Chest", "count": 0, "needed": 1, "text": "Open the chest"}])]),
            my_guid=1)
        self.assertEqual(ids(cands)[0], "interact:guid=70")
        self.assertIn("quest: Open the chest", cands[0]["label"])

    def test_gameobject_out_of_reach_is_walked_to_not_interacted(self):
        obj = self.go(70, 3333, 20.0)
        cands = cand.generate(snap(objects=[obj], quest_log=[quest([{
            "entry": 3333, "gameobject": True, "name": "Chest", "count": 0, "needed": 1, "text": "Open the chest"}])]),
            my_guid=1)
        self.assertEqual(ids(cands)[0], "move_towards:guid=70,stop_distance=3.0")
        self.assertIn("quest: Open the chest", cands[0]["label"])

    def test_completed_gameobject_objective_offers_nothing(self):
        obj = self.go(70, 3333, 3.0)
        cands = cand.generate(snap(objects=[obj], quest_log=[quest([{
            "entry": 3333, "gameobject": True, "name": "Chest", "count": 1, "needed": 1, "text": "Open the chest"}])]),
            my_guid=1)
        self.assertEqual(quest_reasons(cands), [])

    def test_gameobject_beyond_approach_range_is_not_offered(self):
        obj = self.go(70, 3333, 41.0)
        cands = cand.generate(snap(objects=[obj], quest_log=[quest([{
            "entry": 3333, "gameobject": True, "name": "Chest", "count": 0, "needed": 1, "text": "Open the chest"}])]),
            my_guid=1)
        self.assertEqual(quest_reasons(cands), [])


class QuestSequenceScenarioTest(unittest.TestCase):
    """The four-step walk-through of #315: giver, accept, kill, turn in. Each snapshot's top option."""

    def test_find_accept_kill_turn_in(self):
        giver = mob(GIVER, 15278, 3.0, npc_flags=3, quest_giver_status="available", name="Magistrix Erona")
        mobs = [mob(61, 1001, 8), mob(62, 1001, 9), mob(63, 2002, 4)]
        steps = [
            (snap(units=[giver]), "interact:guid=5001"),
            (snap(units=[giver], window={"kind": "quest_details", "npc_guid": GIVER, "quest_id": QUEST,
                                         "title": "Mana Wyrms"}),
             f"accept_quest:npc_guid={GIVER},quest_id={QUEST}"),
            (snap(units=mobs, quest_log=[quest([kill(1001, 0, 3)])]), "move_towards:guid=61,stop_distance=3.0"),
            (snap(units=[giver], window={"kind": "quest_offer_reward", "npc_guid": GIVER, "quest_id": QUEST,
                                         "title": "Mana Wyrms"}, quest_log=[quest([kill(1001, 3, 3)])]),
             f"turn_in_quest:npc_guid={GIVER},quest_id={QUEST},reward_choice=0"),
        ]
        for n, (s, top) in enumerate(steps, 1):
            with self.subTest(step=n):
                cands = cand.generate(s, my_guid=1)
                self.assertEqual(cands[0]["id"], top)
        kill_step = cand.generate(steps[2][0], my_guid=1)
        self.assertEqual(ids(kill_step)[:2], ["move_towards:guid=61,stop_distance=3.0",
                                              "move_towards:guid=62,stop_distance=3.0"])
        self.assertLess(ids(kill_step).index("move_towards:guid=62,stop_distance=3.0"),
                        ids(kill_step).index("auto_attack:guid=63"))


if __name__ == "__main__":
    unittest.main()
