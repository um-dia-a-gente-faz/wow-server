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
The model tier (N samples per scenario against Jev/LLM) is #541.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from agent import brain as br
from agent import config, httpjson, jev
from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent.deciders import ScriptedDecider
from agent.fleetlimits import FleetLimits
from agent.handles import UnknownHandle
from agent.jev import JevError
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


def offering_attack_on(guid):
    return lambda cs: any(c["action"] == "auto_attack" and c["params"].get("guid") == guid for c in cs)


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
    "low_health_does_not_pull_a_mob": dict(
        snapshot=snap([unit(10, 4, level=1)], me=HURT),
        offered=not_offering_guid(10)),
    "hurt_still_fights_back": dict(  # the rule stops pulls, not defence
        snapshot=snap([unit(10, 4, level=1, target_guid=ME, in_combat=True)], me=HURT),
        offered=offering_attack_on(10), chosen=first_is("auto_attack", guid=10)),
    # #544: hurt and in combat with no attacker in view (a stuck combat flag): the pull comes back
    "hurt_in_combat_with_no_attacker_offers_a_pull": dict(
        snapshot=snap([unit(10, 4, level=1)], me=dict(HURT, in_combat=True)),
        offered=offering_attack_on(10), chosen=first_is("auto_attack", guid=10)),
    # #544: hurt, in combat, a real attacker on us: the idle mob is still not pulled
    "hurt_in_a_fight_pulls_no_idle_mob": dict(
        snapshot=snap([unit(10, 4, level=1, target_guid=ME, in_combat=True), unit(11, 4, level=1)],
                      me=dict(HURT, in_combat=True)),
        offered=lambda cs: offering_attack_on(10)(cs) and not_offering_guid(11)(cs),
        chosen=first_is("auto_attack", guid=10)),
    # gh-555: hurt and in combat, the attacker is stunned (target 0) and 10 yd away, so it is not a
    # threat; the idle rats must still not be pulled while a possible attacker is in view
    "hurt_with_a_stunned_attacker_pulls_no_idle_mob": dict(
        snapshot=snap([unit(10, 10, level=1, in_combat=True, target_guid=0), unit(11, 4, level=1),
                       unit(12, 5, level=1), unit(13, 6, level=1)], me=dict(HURT, in_combat=True)),
        offered=lambda cs: any(c["params"].get("guid") == 10 for c in cs)
        and all(c["params"].get("guid") not in (11, 12, 13) for c in cs),
        chosen=first_is("move_towards", guid=10)),
    "healthy_pulls_the_idle_mob": dict(
        snapshot=snap([unit(10, 4, level=1)]),
        offered=offering_attack_on(10), chosen=first_is("auto_attack", guid=10)),
}


class FirstCandidateJev:
    """The scripted brain: candidates are priority-ordered, so take the top."""
    model = "scripted"

    def choose_action(self, snapshot, options, persona="", history=None):
        return options[0]["action"], options[0]["params"]


class _Handles:
    """Stands in for agent.handles.HandleMap: resolves the handle strings of a captured scenario
    through its `handles` literal (handle -> GUID), as the live map resolves its own."""

    def __init__(self, table):
        self.table = table

    def resolve(self, handle):
        if handle not in self.table:
            raise UnknownHandle(f"unknown handle {handle!r}")
        return self.table[handle]


def offered_for(s):
    """The candidate list the brain is given for scenario `s` (`my_guid` and `handles` optional)."""
    handles = _Handles(s["handles"]) if "handles" in s else None
    return cand.generate(s["snapshot"], my_guid=s.get("my_guid", ME), history=s.get("history"), handles=handles)


def violations(s):
    snapshot, history = s["snapshot"], s.get("history")
    offered = offered_for(s)
    d = br.Brain(jev=FirstCandidateJev()).decide(snapshot, my_guid=s.get("my_guid", ME), history=history,
                                                 handles=_Handles(s["handles"]) if "handles" in s else None)
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


class _Boom:
    """A decider that fails the test if it is asked to decide."""
    model = "boom"
    last_usage: dict = {}
    last_latency_ms = 1.0

    def __init__(self):
        self.calls = 0

    def choose_action(self, *a, **kw):
        self.calls += 1
        raise AssertionError("a blocked brain must not be called")


class FleetBlockTest(unittest.TestCase):
    """ADR 0017 slice 1 (#625): Brain asks the fleet limits file before a Jev or an LLM call."""

    NOW = 1_700_000_000.0

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "limits.json")

    def block(self, *brains, until=NOW + 30):
        with open(self.path, "w") as f:
            json.dump({"version": 1, "blocks": {b: {"until_ts": until, "reason": f"{b} stop"} for b in brains}}, f)
        return FleetLimits(self.path, lambda: self.NOW)

    def decide(self, **kw):
        return br.Brain(**kw).decide(snap(), my_guid=ME)

    def test_blocked_jev_is_not_called_and_the_cycle_fails(self):
        jev = _Boom()
        with self.assertRaises(br.BrainError) as cm:
            self.decide(jev=jev, fleet=self.block("jev"))
        self.assertEqual(jev.calls, 0)
        d = cm.exception.decision
        self.assertEqual(d.jev_status, "fleet_blocked")
        self.assertEqual(br.JEV_STATUS_FLEET, "fleet_blocked")
        self.assertEqual(d.brain, "jev")
        self.assertIn("jev blocked by fleet limits until", str(cm.exception))
        self.assertIn("jev stop", d.fallback)
        self.assertIsNone(d.latency_ms)   # else _derive_jev puts it in the Jev latency histogram
        self.assertEqual(d.usage, {})
        self.assertIsNone(d.candidates)

    def test_blocked_jev_substitutes_the_llm_when_configured(self):
        jev, llm = _Boom(), ScriptedDecider([("idle", {})])
        d = self.decide(jev=jev, llm=llm, fleet=self.block("jev"))
        self.assertEqual(jev.calls, 0)
        self.assertEqual((d.brain, d.substituted, d.jev_status), ("llm", True, "fleet_blocked"))
        self.assertIn("jev stop", d.fallback)

    def test_blocked_jev_whose_llm_is_blocked_too_fails_without_a_call(self):
        jev, llm = _Boom(), _Boom()
        with self.assertRaises(br.BrainError) as cm:
            self.decide(jev=jev, llm=llm, fleet=self.block("jev", "llm"))
        self.assertEqual((jev.calls, llm.calls), (0, 0))
        self.assertEqual(cm.exception.decision.jev_status, "fleet_blocked")
        self.assertIn("llm stop", str(cm.exception))

    def test_blocked_llm_is_not_called(self):
        llm = _Boom()
        with self.assertRaises(br.BrainError) as cm:
            self.decide(llm=llm, fleet=self.block("llm"))
        self.assertEqual(llm.calls, 0)
        self.assertIn("llm blocked by fleet limits until", str(cm.exception))
        self.assertEqual(cm.exception.decision.brain, "llm")
        self.assertIsNone(cm.exception.decision.latency_ms)

    def test_a_block_on_the_other_brain_changes_nothing(self):
        d = self.decide(jev=ScriptedDecider([("idle", {})]), fleet=self.block("llm"))
        self.assertEqual((d.brain, d.jev_status), ("jev", "success"))
        d = self.decide(llm=ScriptedDecider([("idle", {})]), fleet=self.block("jev"))
        self.assertEqual(d.brain, "llm")

    def test_an_expired_block_decides_normally(self):
        d = self.decide(jev=ScriptedDecider([("idle", {})]), fleet=self.block("jev", until=self.NOW))
        self.assertEqual(d.jev_status, "success")

    def test_fleet_runs_after_the_cooldown_and_before_the_budget(self):
        b = br.Brain(jev=_Boom(), fleet=self.block("jev"), max_tokens_per_hour=1)
        b.budget.record(10)   # budget spent too: the fleet reason comes first
        with self.assertRaises(br.BrainError) as cm:
            b.decide(snap(), my_guid=ME)
        self.assertEqual(cm.exception.decision.jev_status, "fleet_blocked")
        b._jev_cooldown_until, b._jev_cooldown_reason = b._clock() + 60, "HTTP 429"
        with self.assertRaises(br.BrainError) as cm:   # cooling down: the file is not even asked
            b.decide(snap(), my_guid=ME)
        self.assertNotEqual(cm.exception.decision.jev_status, "fleet_blocked")

    def test_a_broken_limits_file_never_stops_a_decision(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        d = self.decide(jev=ScriptedDecider([("idle", {})]), fleet=FleetLimits(self.path))
        self.assertEqual(d.jev_status, "success")

    def test_unset_means_no_fleet_and_no_file_access(self):
        self.assertIsNone(br.Brain(jev=ScriptedDecider([("idle", {})])).fleet)
        for env in ({}, {"AGENT_FLEET_LIMITS_FILE": ""}):
            with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://llm/v1", "LLM_MODEL": "m", **env}, clear=True):
                cfg = config.Config()
                b = br.Brain.from_config(cfg)
            self.assertEqual(cfg.fleet_limits_file, "")
            self.assertIsNone(b.fleet)
            b.llm = ScriptedDecider([("idle", {})])
            with mock.patch("builtins.open", side_effect=AssertionError("opened a file")), \
                    mock.patch("os.stat", side_effect=AssertionError("stat")), \
                    self.assertNoLogs("agent", level="DEBUG"):
                d = b.decide(snap(), my_guid=ME)
            self.assertEqual(d.brain, "llm")

    def test_from_config_builds_fleet_limits_when_the_setting_is_set(self):
        env = {"AGENT_FLEET_LIMITS_FILE": self.path, "LLM_BASE_URL": "http://llm/v1", "LLM_MODEL": "m"}
        with mock.patch.dict(os.environ, env, clear=True):
            b = br.Brain.from_config(config.Config())
        assert b is not None and b.fleet is not None
        self.assertIsInstance(b.fleet, FleetLimits)
        self.assertEqual(b.fleet.path, self.path)


class FleetCountTest(unittest.TestCase):
    """#675: one fleet-blocked count per cycle and brain, only where the limits file decides."""

    NOW = FleetBlockTest.NOW

    def setUp(self):
        from agent import metrics
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "limits.json")
        self.now = self.NOW
        self.metrics = metrics
        metrics.FLEET_BLOCKED.update(dict.fromkeys(metrics.FLEET_BRAINS, 0))
        self.addCleanup(metrics.FLEET_BLOCKED.update, dict.fromkeys(metrics.FLEET_BRAINS, 0))

    def fleet(self, *brains):
        with open(self.path, "w") as f:
            json.dump({"version": 1, "blocks": {b: {"until_ts": self.NOW + 30, "reason": "x"} for b in brains}}, f)
        return FleetLimits(self.path, lambda: self.now)

    def counts(self):
        return dict(self.metrics.FLEET_BLOCKED)

    def cycle(self, b):
        with self.assertRaises(br.BrainError):
            b.decide(snap(), my_guid=ME)

    def test_jev_blocked_three_cycles_then_expiry(self):
        b = br.Brain(jev=_Boom(), fleet=self.fleet("jev"))
        for _ in range(3):
            self.cycle(b)
        self.now = self.NOW + 31
        b.jev = ScriptedDecider([("idle", {})])
        b.decide(snap(), my_guid=ME)
        self.assertEqual(self.counts(), {"jev": 3, "llm": 0})

    def test_llm_block_counts_under_llm(self):
        self.cycle(br.Brain(llm=_Boom(), fleet=self.fleet("llm")))
        self.assertEqual(self.counts(), {"jev": 0, "llm": 1})

    def test_both_blocked_counts_once_each(self):
        self.cycle(br.Brain(jev=_Boom(), llm=_Boom(), fleet=self.fleet("jev", "llm")))
        self.assertEqual(self.counts(), {"jev": 1, "llm": 1})

    def test_budget_skip_counts_nothing(self):
        b = br.Brain(jev=_Boom(), max_tokens_per_hour=1, fleet=self.fleet())
        b.budget.record(10)
        self.cycle(b)
        self.assertEqual(self.counts(), {"jev": 0, "llm": 0})

    def test_unblocked_llm_cycle_counts_nothing(self):
        br.Brain(llm=ScriptedDecider([("idle", {})]), fleet=self.fleet("jev")).decide(snap(), my_guid=ME)
        self.assertEqual(self.counts(), {"jev": 0, "llm": 0})

    def test_cooldown_skip_counts_nothing(self):
        b = br.Brain(jev=_Boom(), fleet=self.fleet("jev"))
        b._jev_cooldown_until = b._clock() + 60
        self.cycle(b)
        self.assertEqual(self.counts(), {"jev": 0, "llm": 0})


class _ErrJev:
    """A Jev client that raises one scripted JevError and counts its calls."""
    model = "err"
    last_usage: dict = {}
    last_latency_ms = 1.0

    def __init__(self, error):
        self.error, self.calls = error, 0

    def choose_action(self, snapshot, candidates, persona="", history=None):
        self.calls += 1
        raise self.error


class _Rng:
    """uniform(a, b) returns a fixed end of the range (0.0 jitter = "a")."""

    def __init__(self, end="a"):
        self.end = end

    def uniform(self, a, b):
        return a if self.end == "a" else b


class JevCooldownTest(unittest.TestCase):
    """#636: which Jev failures start a cooldown, for how long, and the jitter on it."""

    def make(self, error, end="a", llm=None):
        self.now = [1000.0]
        self.jev = _ErrJev(error)
        return br.Brain(jev=self.jev, llm=llm, clock=lambda: self.now[0], rng=_Rng(end))

    def fail(self, b):
        with self.assertRaises(br.BrainError):
            b.decide(snap(), my_guid=ME)

    def cooldown(self, b):
        return b._jev_cooldown_until - 1000.0

    def test_5xx_and_transient_errors_cool_down_ten_seconds(self):
        errors = [JevError(f"HTTP {s}", status=s) for s in (500, 502, 503, 504)]
        errors.append(JevError("net", transient=True))
        for e in errors:
            with self.subTest(status=e.status):
                b = self.make(e)
                self.fail(b)
                self.assertEqual(self.jev.calls, 1)
                self.assertEqual(self.cooldown(b), 10.0)
                self.now[0] += br.JEV_ERROR_COOLDOWN_S - 1
                self.fail(b)
                self.assertEqual(self.jev.calls, 1)   # still cooling: not called
                self.now[0] += 2
                self.fail(b)
                self.assertEqual(self.jev.calls, 2)

    def test_network_error_from_jev_error_builder_cools_down(self):
        e = jev._jev_error(httpjson.HttpJsonError("network", "http://j", detail="timed out"))
        self.assertTrue(e.transient)
        b = self.make(e)
        self.fail(b)
        self.assertEqual(b._jev_cooldown_reason, "network error")
        self.assertEqual(self.cooldown(b), br.JEV_ERROR_COOLDOWN_S)

    def test_429_retry_after(self):
        for retry_after, want in ((120, 120), (None, 30), (0, 5), (-3, 5), (99999, 900)):
            with self.subTest(retry_after=retry_after):
                b = self.make(JevError("x", status=429, retry_after=retry_after))
                self.fail(b)
                self.assertEqual(self.cooldown(b), want)

    def test_auth_status_ignores_retry_after(self):
        b = self.make(JevError("x", status=402, retry_after=1))
        self.fail(b)
        self.assertEqual(self.cooldown(b), 300)

    def test_other_failures_start_no_cooldown(self):
        for e in (JevError("x", status=400), JevError("x", status=404), JevError("malformed"),
                  jev._jev_error(httpjson.HttpJsonError("non_json", "http://j", body=b"<html>"))):
            with self.subTest(e=str(e)):
                b = self.make(e)
                self.fail(b)
                self.fail(b)
                self.assertEqual(self.jev.calls, 2)

    def test_a_shorter_cooldown_never_replaces_a_longer_one(self):
        b = self.make(JevError("x", status=402))
        self.fail(b)
        b.jev.error = JevError("net", transient=True)
        self.now[0] += 5
        b._maybe_cool_down(b.jev.error)
        self.assertEqual(self.cooldown(b), 300)
        self.assertEqual(b._jev_cooldown_reason, "HTTP 402")

    def test_a_longer_cooldown_replaces_a_shorter_one(self):
        b = self.make(JevError("net", transient=True))
        self.fail(b)
        b._maybe_cool_down(JevError("x", status=402))
        self.assertEqual((self.cooldown(b), b._jev_cooldown_reason), (300, "HTTP 402"))

    def test_jitter_end_of_range(self):
        b = self.make(JevError("x", status=429), end="b")
        self.fail(b)
        self.assertAlmostEqual(self.cooldown(b), 33.0)

    def test_jitter_is_applied_after_the_clamp(self):
        # 99999 s clamps to 900 s first, then the 10 % jitter: 990 s, not 900 (jitter
        # lost to the clamp) and not 109 998.9 (clamp lost to the jitter).
        b = self.make(JevError("x", status=429, retry_after=99999), end="b")
        self.fail(b)
        self.assertAlmostEqual(self.cooldown(b), 990.0)

    def test_jitter_never_below_base(self):
        import random
        b = self.make(JevError("x", status=429))
        b._rng = random.Random(7)
        for _ in range(1000):
            b._jev_cooldown_until = 0.0
            b._maybe_cool_down(b.jev.error)
            self.assertGreaterEqual(self.cooldown(b), 30.0)
            self.assertLessEqual(self.cooldown(b), 33.0)

    def test_brains_do_not_share_a_generator(self):
        self.assertIsNot(br.Brain(jev=_Boom())._rng, br.Brain(jev=_Boom())._rng)

    def test_a_network_cooldown_is_substituted_by_the_llm(self):
        b = self.make(JevError("net", transient=True), llm=ScriptedDecider([("idle", {})]))
        d = b.decide(snap(), my_guid=ME)
        self.assertTrue(d.substituted)
        d = b.decide(snap(), my_guid=ME)   # cooling: Jev not called, the LLM stands in
        self.assertEqual((self.jev.calls, d.brain, d.substituted), (1, "llm", True))


if __name__ == "__main__":
    unittest.main()
