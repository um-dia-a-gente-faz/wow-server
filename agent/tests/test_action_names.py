"""gh-289: agent/action_names.py and agent.actions.REGISTRY agree, candidates.py spells no action
name itself, and every candidate (fixtures and the golden cases) resolves and validates."""

import ast
import json
import pathlib
import unittest

from agent import action_names as names
from agent import actions as ac
from agent import candidates as cand
from agent import death  # noqa: F401  registers release_spirit/reclaim_corpse
from agent.reflexes import follow, rest  # noqa: F401  register follow/assist/stop_following/rest

HERE = pathlib.Path(__file__).parent
CANDIDATES_PY = HERE.parent / "candidates.py"


def _constants() -> dict:
    return {k: v for k, v in vars(names).items() if k.isupper()}


class ActionNamesTest(unittest.TestCase):
    def test_names_match_registry_both_ways(self):
        consts = _constants()
        self.assertEqual(set(consts.values()), set(ac.REGISTRY))
        for const, value in consts.items():
            self.assertEqual(const, value.upper())

    def test_not_offered_only_names_registered_actions(self):
        self.assertLessEqual(set(cand.NOT_OFFERED), set(ac.REGISTRY))

    def test_candidates_py_has_no_action_name_literals(self):
        tree = ast.parse(CANDIDATES_PY.read_text())
        skip = set()
        for node in ast.walk(tree):
            # docstrings, and dict lookups like reflex_state.get("follow") (a state key, not an action)
            if isinstance(node, (ast.Module, ast.FunctionDef)) and node.body \
                    and isinstance(node.body[0], ast.Expr):
                skip.add(id(node.body[0].value))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                skip.update(id(a) for a in node.args)
        found = [(n.lineno, n.value) for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and n.value in ac.REGISTRY and id(n) not in skip]
        self.assertEqual(found, [])

    def test_golden_candidates_resolve_and_validate(self):
        golden = json.loads((HERE / "fixtures" / "golden" / "actions_golden.json").read_text())
        count = 0
        for case, cands in golden["candidates"].items():
            for c in cands:
                action = ac.REGISTRY.get(c["action"])
                self.assertIsNotNone(action, f"{case}: {c['action']} is not registered")
                self.assertLessEqual(set(c["params"]), set(action.params), f"{case}: {c}")
                self.assertLessEqual(set(action.required), set(c["params"]), f"{case}: {c}")
                count += 1
        self.assertGreater(count, 50)


if __name__ == "__main__":
    unittest.main()
