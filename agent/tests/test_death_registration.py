"""gh-321: release_spirit / reclaim_corpse must be in REGISTRY in a fresh process.

agent/death.py registers them as an import side effect and nothing in production imported
it, so the live agent answered "unknown action: reclaim_corpse" and a dead character could
never release. Test modules that `import death` hide this, so each check runs in a clean
interpreter that imports only the production entry points."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _registry_in_fresh_process(module: str) -> set:
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            f"import {module}, agent.actions as a; print(' '.join(sorted(a.REGISTRY)))")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         check=True, cwd=ROOT)
    return set(out.stdout.split())


class DeathRegistrationTest(unittest.TestCase):
    def test_entry_point_registers_death_actions(self):
        self.assertLessEqual({"release_spirit", "reclaim_corpse"}, _registry_in_fresh_process("agent.__main__"))

    def test_think_alone_registers_death_actions(self):
        self.assertLessEqual({"release_spirit", "reclaim_corpse"}, _registry_in_fresh_process("agent.think"))


if __name__ == "__main__":
    unittest.main()
