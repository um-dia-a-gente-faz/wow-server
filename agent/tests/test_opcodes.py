"""agent/opcodes.py is the single opcode table (#247)."""

import ast
import collections
import pathlib
import unittest

from agent import opcodes
from agent import session  # noqa: F401  (importing registers every handler on ROUTER)
from agent.router import ROUTER

AGENT = pathlib.Path(opcodes.__file__).parent
PREFIXES = ("SMSG_", "CMSG_", "MSG_")


def _table():
    return {n: v for n, v in vars(opcodes).items() if n.startswith(PREFIXES)}


class OpcodeTableTest(unittest.TestCase):
    def test_no_duplicate_names(self):
        names = [t.targets[0].id for t in ast.parse(pathlib.Path(opcodes.__file__).read_text()).body
                 if isinstance(t, ast.Assign)]
        dup = [n for n, c in collections.Counter(names).items() if c > 1]
        self.assertEqual(dup, [])

    def test_no_duplicate_values(self):
        by_value = collections.defaultdict(list)
        for n, v in _table().items():
            by_value[v].append(n)
        self.assertEqual({hex(v): n for v, n in by_value.items() if len(n) > 1}, {})

    def test_every_routed_opcode_is_defined(self):
        known = set(_table().values())
        self.assertEqual(sorted(hex(o) for o in ROUTER._handlers if o not in known), [])

    def test_no_opcode_defined_outside_opcodes_py(self):
        stray = []
        for path in AGENT.rglob("*.py"):
            if path.name == "opcodes.py" or "tests" in path.parts:
                continue
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    stray += [f"{path.name}:{t.id}" for t in node.targets
                              if isinstance(t, ast.Name) and t.id.startswith(PREFIXES)]
        self.assertEqual(stray, [])


if __name__ == "__main__":
    unittest.main()
