"""Pin the order and the caps of `candidates.generate` (gh-503): busy snapshots where every provider
emits more than its cap, with a limit that truncates nothing, against the exact id lists that
`origin/main` produced before candidates.py was split into providers. Regenerate deliberately with
`python3 -m agent.tests.test_candidate_order --write` (from a tree whose generate you trust)."""

import json
import pathlib
import sys
import unittest

from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent.reflexes import follow  # noqa: F401

EXPECTED = pathlib.Path(__file__).parent / "fixtures" / "candidates" / "provider_order.json"
GEAR = {"inventory_type": 5, "class_": 4, "quality": 1, "item_level": 20, "allowable_class": -1,
        "subclass": 1, "stats": []}
WINDOWS = {
    "none": None,
    "trainer": {"kind": "trainer", "npc_guid": 8, "spells": [{"spell_id": 900 + i} for i in range(5)]},
    "vendor": {"kind": "vendor", "npc_guid": 8},
    "gossip": {"kind": "gossip", "npc_guid": 8, "quests": [{"quest_id": 1, "title": "q"}],
               "options": [{"index": i, "text": f"o{i}"} for i in range(6)]},
}


def busy(window: dict | None) -> dict:
    """Every provider has more to offer than its cap."""
    def unit(guid, distance, **kw):
        return {"guid": guid, "distance": distance, "level": 3, "name": f"u{guid}", **kw}

    units = (
        [unit(10 + i, 3 + i, in_combat=True, target_guid=1) for i in range(5)]                 # threats
        + [unit(20 + i, d, lootable=True) for i, d in enumerate((2, 6, 10, 12, 14))]            # loot
        + [unit(30 + i, 2 + 2 * i, npc_flags=2, quest_giver_status="available") for i in range(5)]
        + [unit(40 + i, 6 + 3 * i) for i in range(5)])                                         # attack
    snap = {
        "me": {"level": 5, "class_id": 8, "health": "10/100", "mana": "10/100"},
        "spells": [{"id": i} for i in (635, 635, 635, 2050, 133, 585, 589, 116)],
        "inventory": ([{"slot": 23 + i, "template": {"quality": 1, "spells": [{"trigger": 0}]}} for i in range(4)]
                      + [{"slot": 27 + i, "template": dict(GEAR)} for i in range(5)]
                      + [{"slot": 32 + i, "template": {"quality": 0}} for i in range(5)]),
        "equipment": {},
        "pending_invite": {"inviter_name": "Bob"},
        "nearby_units": units,
        "nearby_players": [{"name": n, "distance": d} for n, d in (("Ann", 4), ("Bob", 2), ("Cy", 9), ("Di", 6))],
        "quest_log": [{"quest_id": i, "slot": i, "state_name": "failed", "title": f"f{i}"} for i in range(4)],
        "window": window,
    }
    return snap


def cases() -> dict:
    out = {}
    for wname, window in WINDOWS.items():
        out[f"busy_{wname}"] = (busy(window), {})
        out[f"busy_{wname}_following"] = (busy(window), {"reflex_state": {"follow": {
            "enabled": True, "leader_name": "Ann"}}})
    return out


def current() -> dict:
    return {name: [c["id"] for c in cand.generate(snap, my_guid=1, limit=200, **kw)]
            for name, (snap, kw) in cases().items()}


class ProviderOrderTest(unittest.TestCase):
    def test_busy_snapshots_keep_main_order_and_caps(self):
        expected = json.loads(EXPECTED.read_text())
        got = current()
        self.assertEqual(set(got), set(expected))
        for name in expected:
            self.assertEqual(got[name], expected[name], name)

    def test_the_busy_snapshots_really_hit_every_cap(self):
        ids = current()

        def count(name, prefix):
            return sum(i.startswith(prefix) for i in ids[name])
        self.assertEqual(count("busy_none", "cast_spell:spell_id=133,") + count("busy_none", "cast_spell:spell_id=585,")
                         + count("busy_none", "cast_spell:spell_id=589,"), 3)
        self.assertEqual(count("busy_trainer", "train_spell"), cand.MAX_TRAIN)
        self.assertEqual(count("busy_vendor", "sell_item"), cand.MAX_SELL)
        self.assertEqual(count("busy_gossip", "gossip_select"), cand.MAX_GOSSIP_OPTIONS)
        self.assertEqual(count("busy_none", "equip_item"), cand.MAX_EQUIP)
        self.assertEqual(count("busy_none", "use_item"), cand.MAX_USE_ITEMS)
        self.assertEqual(count("busy_none", "abandon_quest"), cand.MAX_ABANDON)
        self.assertEqual(count("busy_none", "follow:"), cand.MAX_FOLLOW)
        self.assertEqual(ids["busy_none"][0].split(":")[0], "cast_spell")  # a heal goes first


class DeadPathTest(unittest.TestCase):
    """The dead providers read only the dead flags and corpse_position, as before the split."""

    def test_malformed_alive_only_fields_do_not_stop_a_dead_agent_releasing(self):
        for snap in ({"is_dead": True, "nearby_units": [None], "me": "x"},
                     {"is_dead": True, "nearby_units": 5, "me": 3, "spells": 1, "inventory": "z"}):
            self.assertEqual([c["action"] for c in cand.generate(snap, my_guid=1)], ["release_spirit", "idle"])

    def test_ghost_with_malformed_alive_only_fields_reclaims(self):
        snap = {"is_ghost": True, "nearby_units": [None], "me": "x"}
        self.assertEqual([c["action"] for c in cand.generate(snap)], ["reclaim_corpse", "idle"])

    def test_history_never_drops_the_dead_options(self):
        history = [{"action": "release_spirit", "args": {}, "ok": False, "error": "bad params: x"}] * 3
        self.assertEqual([c["id"] for c in cand.generate({"is_dead": True}, history=history)],
                         ["release_spirit", "idle"])
        history = [{"action": "reclaim_corpse", "args": {}, "ok": False, "error": "bad params: x"}] * 3
        self.assertEqual([c["action"] for c in cand.generate({"is_ghost": True}, history=history)],
                         ["reclaim_corpse", "idle"])


if __name__ == "__main__":
    if "--write" in sys.argv:
        EXPECTED.write_text(json.dumps(current(), indent=1) + "\n")
    else:
        unittest.main()
