"""#717: no test edits a tracked file of the checkout in place.

test_generate used to rewrite agent/opcodes.py and run `generate.main([])` against the real
repo; a second process importing agent meanwhile cached the edited module, and a killed run left
the checkout modified. Tests build a temporary tree instead.

What this sees (AST, per module, over every tracked tests/test_*.py): a `generate.main(...)`
whose argument list lacks "--check", and a `.write_text/.write_bytes/.unlink` call whose receiver
mentions ROOT/REPO or a name assigned (in the same module) from an expression mentioning them.
Code under `if __name__ == "__main__"` (the golden --write modes) is skipped. It cannot see a
path built in another module, via a helper, or by a subprocess; it is a tripwire, not a proof.
"""
import ast
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REAL_ROOTS = {"ROOT", "REPO", "REPO_ROOT"}
MUTATORS = {"write_text", "write_bytes", "unlink"}


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def violations(source: str) -> list[str]:
    if "generate" not in source and not any(m in source for m in MUTATORS):
        return []  # cheap prefilter, parsing every test module costs a second
    tree = ast.parse(source)
    main_blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If) and "__name__" in _names(n.test)]
    skipped = {id(c) for b in main_blocks for c in ast.walk(b)}
    real = set(REAL_ROOTS)
    for _ in range(3):  # a name assigned from the root, or from such a name, is the root's too
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and _names(n.value) & real:
                real |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    found = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or id(n) in skipped or not isinstance(n.func, ast.Attribute):
            continue
        f = n.func
        if f.attr == "main" and isinstance(f.value, ast.Name) and f.value.id == "generate":
            if not any(isinstance(c, ast.Constant) and c.value == "--check" for a in n.args for c in ast.walk(a)):
                found.append(f"line {n.lineno}: generate.main() in write mode")
        elif f.attr in MUTATORS and _names(f.value) & real:
            found.append(f"line {n.lineno}: .{f.attr}() on a path under the repository root")
    return found


class NoCheckoutWritesTest(unittest.TestCase):
    def test_guard_flags_the_old_test_generate(self):
        old = ('ROOT = 1\ndef t():\n    doc = ROOT / "docs/AGENT-API.md"\n    doc.write_text("x")\n'
               '    generate.main([])\n    generate.main(["--check"])\n')
        self.assertEqual(len(violations(old)), 2)
        fixed = old.replace("doc.write_text", "tmp.write_text").replace("main([])", "main(['--check'])")
        self.assertEqual(violations(fixed), [])

    def test_no_test_modifies_the_checkout(self):
        files = subprocess.run(["git", "ls-files", "*/tests/test_*.py"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.split()
        self.assertTrue(files)
        bad = {f: v for f in files if (v := violations((ROOT / f).read_text()))}
        self.assertEqual(bad, {}, "tests must work on a temporary copy, never edit tracked files in place")


if __name__ == "__main__":
    unittest.main()
