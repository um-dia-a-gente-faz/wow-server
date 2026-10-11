"""gh-419: agent.action_specs is the one source of an action's name, params, required params and
cap. The registered classes, the tool schema, catalog() and think.py's validation all read it; a
spec that disagrees with its class fails here."""

import dataclasses
import inspect
import pathlib
import re
import unittest
from unittest import mock

from agent import action_specs as specs
from agent import actions as ac
from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent import perception as per
from agent.reflexes import follow, rest  # noqa: F401  register follow/assist/stop_following/rest
from agent.tests.builders import FakeSession
from agent.tests.test_think import FakeLLMClient, object_at
from agent.think import think_and_act

AGENT = pathlib.Path(__file__).resolve().parents[1]


def disagreements(registry=None) -> list[str]:
    """Every way a spec and its registered class differ: name set, params, required."""
    registry = ac.REGISTRY if registry is None else registry
    out = [f"{n}: registered without a spec" for n in registry if n not in specs.specs_by_name]
    out += [f"{n}: spec without a registered action" for n in specs.specs_by_name if n not in registry]
    for name, action in registry.items():
        spec = specs.specs_by_name.get(name)
        if spec is None:
            continue
        if action.params != spec.params or list(action.params) != list(spec.params):
            out.append(f"{name}: params differ")
        if tuple(action.required) != spec.required:
            out.append(f"{name}: required differs")
    return out


def _named(fn) -> set[str]:
    skip = {"self", "session", "world"}
    return {n for n, p in inspect.signature(fn).parameters.items()
            if n not in skip and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}


def signature_disagreements(registry=None) -> list[str]:
    """The class's own code against its spec: execute() names exactly the spec's params,
    check() names a subset of them."""
    registry = ac.REGISTRY if registry is None else registry
    out = []
    for name, action in registry.items():
        spec = specs.specs_by_name[name]
        names = set(spec.params)
        if _named(action.execute) != names:
            out.append(f"{name}: execute params {sorted(_named(action.execute))} != spec {sorted(names)}")
        if not _named(action.check) <= names:
            out.append(f"{name}: check takes params outside the spec")
    return out


def _session_and_world():
    world = per.WorldState()
    world.set_my_guid(1)
    world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
    return FakeSession(player_position=(0, 0.0, 0.0, 0.0, 0.0)), world


class SpecVsRegistryTest(unittest.TestCase):
    def test_every_action_signature_matches_its_spec(self):
        self.assertEqual(signature_disagreements(), [])

    def test_an_execute_that_gains_or_loses_a_param_fails(self):
        class Gains(ac.SetTargetAction):
            def execute(self, session, world, guid, mode: int = 0, **_):
                return ac.ActionResult(ok=True)

        class Loses(ac.MoveToAction):
            def execute(self, session, world, x, y, z=None, **_):
                return ac.ActionResult(ok=True)

        class CheckExtra(ac.SetTargetAction):
            def check(self, session, world, guid, extra=None, **_):
                return None

        for cls, name in ((Gains, "set_target"), (Loses, "move_to"), (CheckExtra, "set_target")):
            with self.subTest(cls.__name__):
                self.assertEqual(len(signature_disagreements({name: cls()})), 1)

    def test_every_spec_matches_its_registered_action(self):
        self.assertEqual(disagreements(), [])

    def test_a_spec_whose_params_change_fails_the_check(self):
        spec = specs.specs_by_name[specs.SET_TARGET]
        changed = dataclasses.replace(spec, params={**spec.params, "extra": {"type": "string"}})
        with mock.patch.dict(specs.specs_by_name, {specs.SET_TARGET: changed}):
            self.assertEqual(disagreements(), ["set_target: params differ"])

    def test_a_spec_whose_required_changes_fails_the_check(self):
        spec = specs.specs_by_name[specs.SET_TARGET]
        with mock.patch.dict(specs.specs_by_name, {specs.SET_TARGET: dataclasses.replace(spec, required=())}):
            self.assertEqual(disagreements(), ["set_target: required differs"])

    def test_param_order_is_part_of_the_check(self):
        spec = specs.specs_by_name[specs.FACE]
        reordered = dataclasses.replace(spec, params=dict(reversed(list(spec.params.items()))))
        with mock.patch.dict(specs.specs_by_name, {specs.FACE: reordered}):
            self.assertEqual(disagreements(), ["face: params differ"])


class GeneratedFromSpecsTest(unittest.TestCase):
    def test_tool_schema_and_catalog_come_from_the_spec(self):
        spec = specs.specs_by_name[specs.SET_TARGET]
        changed = dataclasses.replace(spec, params={"guid": {"type": "string", "description": "changed"}})
        with mock.patch.dict(specs.specs_by_name, {specs.SET_TARGET: changed}):
            entry = next(e for e in ac.catalog() if e["name"] == "set_target")
        self.assertEqual(entry["parameters"]["properties"]["guid"]["description"], "changed")
        self.assertEqual(entry["description"], ac.REGISTRY["set_target"].description)

    def test_catalog_lists_every_spec_once(self):
        self.assertEqual(sorted(e["name"] for e in ac.catalog()), sorted(specs.specs_by_name))

    def test_caps_are_defined_only_in_the_specs(self):
        self.assertEqual(cand.MAX_SELL, specs.cap_of("sell_item"))
        self.assertEqual(cand.MAX_EQUIP, specs.cap_of("equip_item"))
        text = (AGENT / "candidates.py").read_text()
        from_specs = set(re.findall(r"^(MAX_[A-Z_]+) = cap_of\(", text, re.M))
        hardcoded = set(re.findall(r"^(MAX_[A-Z_]+) = \d", text, re.M))
        self.assertEqual(from_specs, {"MAX_LOOT", "MAX_QUEST_GIVERS", "MAX_FOLLOW", "MAX_GOSSIP_OPTIONS",
                                      "MAX_ABANDON", "MAX_SPELLS", "MAX_TRAIN", "MAX_EQUIP", "MAX_USE_ITEMS",
                                      "MAX_SELL"})
        self.assertEqual(hardcoded, {"MAX_CANDIDATES", "MAX_THREATS", "MAX_ATTACK"})
        for path in AGENT.rglob("*.py"):
            if "tests" not in path.parts and path.name != "candidates.py":
                self.assertNotRegex(path.read_text(), r"^MAX_(SELL|EQUIP|TRAIN|USE_ITEMS|LOOT) = ", str(path))


class ValidationReadsSpecTest(unittest.TestCase):
    def test_missing_required_param_text(self):
        sess, world = _session_and_world()
        result = think_and_act(sess, world, FakeLLMClient("set_target", {}), my_position=sess.player_position)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing required params: ['guid']")

    def test_required_comes_from_the_spec_not_the_class(self):
        sess, world = _session_and_world()
        spec = specs.specs_by_name[specs.SET_TARGET]
        with mock.patch.dict(specs.specs_by_name, {specs.SET_TARGET: dataclasses.replace(spec, required=())}):
            result = think_and_act(sess, world, FakeLLMClient("set_target", {}), my_position=sess.player_position)
        self.assertNotIn("missing required params", result.error or "")

    def test_unknown_param_text(self):
        """An action whose signature has no **_ rejects an unknown param in execute(), as on main."""
        class Strict(ac.Action):
            name = specs.IDLE
            description = "strict"

            def execute(self, session, world):
                return ac.ActionResult(ok=True)

        sess, world = _session_and_world()
        result = think_and_act(sess, world, FakeLLMClient("idle", {"bogus": 1}),
                               my_position=sess.player_position, registry={"idle": Strict()})
        self.assertFalse(result.ok)
        self.assertRegex(result.error, r"^bad params: .*unexpected keyword argument 'bogus'")


if __name__ == "__main__":
    unittest.main()
