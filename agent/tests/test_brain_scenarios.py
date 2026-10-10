"""Golden brain scenarios (#388), deterministic tier: a hand-built snapshot
plus predicates, not exact strings. Each scenario runs through the real
`Brain.decide` (candidate generation, history, confidence policy) with a
scripted Jev that always takes the first, highest-priority candidate.

A scenario asserts two things:
- `offered`: a predicate on the candidate list the brain is given (it also
  bounds what any model could choose);
- `chosen`: a predicate on the (action, params) the scripted brain picks.

Add one after a live incident: copy the snapshot, state what must never
happen. `known_gap=True` marks a scenario the code does not yet satisfy; the
test then asserts it still fails, so fixing the code forces the flag off.
ponytail: model tier (N samples per scenario against Jev/LLM) and the audit
record -> scenario capture command are not built yet.
"""

import unittest

from agent import brain as br
from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent.reflexes import follow  # noqa: F401  registers follow/assist/stop_following

ME = 1
SELF_ID = {"level": 3, "health": "90/90", "mana": "100/100"}


def unit(guid, distance, level=3, **kw):
    return {"guid": guid, "entry": 1, "name": f"Mob{guid}", "type": "unit", "distance": distance,
            "level": level, "health_pct": 1.0, "in_combat": False, **kw}


def snap(units=(), me=SELF_ID, **kw):
    return {"is_dead": False, "is_ghost": False, "corpse_position": None, "nearby_units": list(units),
            "nearby_players": [], "window": None, "inventory": [], "equipment": {}, "spells": [],
            "quest_log": [], "pending_invite": None, "me": me, **kw}


def never(*actions):
    return lambda cs: all(c["action"] not in actions for c in cs)


def first_is(action, **params):
    return lambda a, p: a == action and all(p.get(k) == v for k, v in params.items())


def not_offering_guid(guid):
    return lambda cs: all(c["params"].get("guid") != guid for c in cs)


def failed(action, args, error):
    return {"action": action, "args": args, "ok": False, "error": error}


POTION = {"slot": 23, "entry": 118, "name": "Healing Potion",
          "template": {"quality": 1, "spells": [{"trigger": 0}]}}
HURT = {"level": 3, "health": "18/90", "mana": "100/100"}
CORPSE = dict(health_pct=0.0, lootable=True)

SCENARIOS = {
    "dead_releases_spirit": dict(
        snapshot=snap(is_dead=True, units=[unit(10, 3)]),
        offered=never("auto_attack", "move_towards", "loot"), chosen=first_is("release_spirit")),
    "ghost_runs_back_never_fights": dict(  # #321 stayed dead
        snapshot=snap(is_ghost=True, units=[unit(10, 3)]),
        offered=never("auto_attack", "move_towards", "release_spirit"), chosen=first_is("reclaim_corpse")),
    "attacked_fights_back": dict(
        snapshot=snap([unit(10, 3, target_guid=ME, in_combat=True), unit(11, 6)]),
        chosen=first_is("auto_attack", guid=10)),
    "attacker_out_of_reach_is_approached_not_ignored": dict(
        snapshot=snap([unit(10, 12, target_guid=ME, in_combat=True)]),
        chosen=first_is("move_towards", guid=10)),
    "never_pull_a_much_stronger_mob": dict(
        snapshot=snap([unit(10, 8, level=10)]),
        offered=not_offering_guid(10), chosen=first_is("idle")),
    "never_pull_with_unknown_own_level": dict(
        snapshot=snap([unit(10, 4)], me={"health": "90/90"}),
        offered=never("auto_attack", "move_towards"), chosen=first_is("idle")),
    "never_steal_someone_elses_fight": dict(
        snapshot=snap([unit(10, 4, in_combat=True, target_guid=77)]),
        offered=not_offering_guid(10), chosen=first_is("idle")),
    "loot_the_corpse_in_reach": dict(
        snapshot=snap([unit(10, 3, **CORPSE), unit(11, 9)]),
        chosen=first_is("loot", guid=10)),
    "walk_to_quest_reward_giver": dict(
        snapshot=snap([unit(10, 20, npc_flags=2, quest_giver_status="reward")]),
        chosen=first_is("move_towards", guid=10)),
    "accept_the_open_quest_dialog": dict(
        snapshot=snap(window={"kind": "quest_details", "npc_guid": 10, "quest_id": 5, "title": "T"}),
        chosen=first_is("accept_quest", quest_id=5)),
    "low_health_drinks_potion_before_pulling": dict(
        snapshot=snap([unit(10, 4)], me=HURT, inventory=[POTION]),
        chosen=first_is("use_item")),
    "do_not_repeat_a_rejected_action": dict(
        snapshot=snap([unit(10, 3, **CORPSE), unit(11, 4, **CORPSE)]),
        history=[failed("loot", {"guid": 10}, "bad params: guid")],
        offered=not_offering_guid(10), chosen=first_is("loot", guid=11)),
    "do_not_fail_the_same_way_twice": dict(
        snapshot=snap([unit(10, 3, **CORPSE)]),
        history=[failed("loot", {"guid": 10}, "too far")] * 2,
        offered=not_offering_guid(10), chosen=first_is("idle")),
    "no_effect_repeat_is_demoted_not_removed": dict(
        snapshot=snap([unit(10, 3, **CORPSE), unit(11, 4, **CORPSE)]),
        history=[{"action": "loot", "args": {"guid": 10}, "ok": True, "changed": False}] * 2,
        offered=lambda cs: [c["id"] for c in cs if c["action"] == "loot"] == ["loot:guid=11", "loot:guid=10"],
        chosen=first_is("loot", guid=11)),
    "far_loot_is_approached": dict(
        snapshot=snap([unit(10, 20, **CORPSE)]),
        offered=never("loot"), chosen=first_is("move_towards", guid=10)),
    "old_rejection_expires": dict(
        snapshot=snap([unit(10, 3, **CORPSE)]),
        history=[failed("loot", {"guid": 10}, "bad params: guid")]
        + [{"action": "idle", "args": {}, "ok": True, "changed": True}] * 4,
        chosen=first_is("loot", guid=10)),
    # Gap found writing this suite: nothing stops an unprovoked pull at 20% HP
    # when no consumable is on hand. Drop known_gap when candidates.py fixes it.
    "low_health_does_not_pull_a_mob": dict(
        snapshot=snap([unit(10, 4, level=1)], me=HURT),
        offered=not_offering_guid(10), known_gap=True),
}


class FirstCandidateJev:
    """The scripted brain: candidates are priority-ordered, so take the top."""
    model = "scripted"

    def choose_action(self, snapshot, options, persona="", history=None):
        return options[0]["action"], options[0]["params"]


def violations(s):
    snapshot, history = s["snapshot"], s.get("history")
    offered = cand.generate(snapshot, my_guid=ME, history=history)
    d = br.Brain(jev=FirstCandidateJev()).decide(snapshot, my_guid=ME, history=history)
    out = []
    if "offered" in s and not s["offered"](offered):
        out.append(f"offered {[c['id'] for c in offered]}")
    if "chosen" in s and not s["chosen"](d.action, d.params):
        out.append(f"chose {d.action} {d.params}")
    return out


class BrainScenarioTest(unittest.TestCase):
    def test_scenarios(self):
        for name, s in SCENARIOS.items():
            with self.subTest(name):
                if s.get("known_gap"):
                    self.assertTrue(violations(s), f"{name} now passes: drop known_gap")
                else:
                    self.assertEqual(violations(s), [])

    def test_at_least_ten_enforced_scenarios(self):
        self.assertGreaterEqual(sum(not s.get("known_gap") for s in SCENARIOS.values()), 10)

    def test_a_wrong_brain_is_caught(self):
        s = dict(SCENARIOS["never_pull_a_much_stronger_mob"], chosen=first_is("auto_attack"))
        self.assertTrue(violations(s))
        s = dict(SCENARIOS["never_pull_a_much_stronger_mob"], snapshot=snap([unit(10, 8, level=3)]))
        self.assertTrue(violations(s), "an in-reach same-level mob must be offered, so the predicate bites")


if __name__ == "__main__":
    unittest.main()
