"""gh-321: every action name must be in REGISTRY in a fresh process.

agent/death.py registers release_spirit / reclaim_corpse as an import side effect and nothing in
production imported it, so the live agent answered "unknown action: reclaim_corpse" and a dead
character could never release. Test modules that import the action modules hide this, so the
check runs in a clean interpreter that imports only the production entry point, and covers every
constant in agent.action_names, not just the death ones."""

import subprocess
import sys
import unittest
from pathlib import Path

from agent import action_names

ROOT = Path(__file__).resolve().parents[2]


def _registry_in_fresh_process(module: str) -> set:
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            f"import {module}, agent.actions as a; print(' '.join(sorted(a.REGISTRY)))")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         check=True, cwd=ROOT)
    return set(out.stdout.split())


def _all_action_names() -> set:
    return {v for k, v in vars(action_names).items() if k.isupper() and isinstance(v, str)}


class ActionRegistrationTest(unittest.TestCase):
    def test_entry_point_registers_death_actions(self):
        self.assertLessEqual({"release_spirit", "reclaim_corpse"}, _registry_in_fresh_process("agent.__main__"))

    def test_login_module_registers_death_actions(self):
        # #292: tools (probe, soak, the world_mock tests) log in through agent.login without agent.__main__.
        self.assertLessEqual({"release_spirit", "reclaim_corpse"}, _registry_in_fresh_process("agent.login"))

    def test_entry_point_registers_every_action_name(self):
        names = _all_action_names()
        self.assertGreater(len(names), 40)
        missing = sorted(names - _registry_in_fresh_process("agent.__main__"))
        self.assertEqual([], missing, "action names with no registered action in the entry point")


if __name__ == "__main__":
    unittest.main()
