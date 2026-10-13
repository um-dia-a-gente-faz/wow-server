"""#717, #725: no test edits a tracked file of the checkout in place.

test_generate used to rewrite agent/opcodes.py and run `generate.main([])` against the real
repo; a second process importing agent meanwhile cached the edited module, and a killed run left
the checkout modified. Tests build a temporary tree instead.

What this sees (AST, per module, over every tracked tests/test_*.py):
  * a `generate.main(...)` whose argument list lacks "--check";
  * a write on a path under the repository root: `.write_text`, `.write_bytes`, `.unlink` on it,
    `open(<path>, <mode>)` and `<path>.open(<mode>)` with a literal mode holding w, a, x or +.
    The root is ROOT, REPO, REPO_ROOT or `__file__` (as in `Path(__file__).resolve().parents[2]`),
    or a name assigned from one of those in the same module.
Code under `if __name__ == "__main__"` (the golden --write modes) is skipped.

It cannot see: shutil.copy/copytree/rmtree/move, os.remove/rename/replace or Path.touch/mkdir
onto the checkout; io.open or os.open for writing; a subprocess that runs a generator or script in write mode; a path built in
another module or by a helper; an alias bound by a `for` loop target; a mode that is not a string
literal. It is a tripwire, not a proof.
"""
import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REAL_ROOTS = {"ROOT", "REPO", "REPO_ROOT", "__file__"}
MUTATORS = {"write_text", "write_bytes", "unlink"}
WRITE_MODE = re.compile(r"[wax+]")


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _mode(call: ast.Call, index: int):
    """The literal mode of an open() call: positional `index`, else the mode= keyword."""
    if len(call.args) > index:
        node = call.args[index]
    else:
        node = next((k.value for k in call.keywords if k.arg == "mode"), None)
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def violations(source: str) -> list[str]:
    if "generate" not in source and not any(r in source for r in REAL_ROOTS):
        return []  # cheap prefilter, parsing every test module costs a second; a checkout write names a root
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    main_blocks = [n for n in nodes if isinstance(n, ast.If) and "__name__" in _names(n.test)]
    skipped = {id(c) for b in main_blocks for c in ast.walk(b)}
    assigns = [(_names(n.value), [t.id for t in n.targets if isinstance(t, ast.Name)])
               for n in nodes if isinstance(n, ast.Assign)]
    real = set(REAL_ROOTS)
    for _ in range(3):  # a name assigned from the root, or from such a name, is the root's too
        for value_names, targets in assigns:
            if value_names & real:
                real.update(targets)
    found = []
    for n in nodes:
        if not isinstance(n, ast.Call) or id(n) in skipped:
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "open" and n.args and _names(n.args[0]) & real:
            mode = _mode(n, 1)
            if mode and WRITE_MODE.search(mode):
                found.append(f"line {n.lineno}: open(..., {mode!r}) on a path under the repository root")
        elif not isinstance(f, ast.Attribute):
            continue
        elif f.attr == "main" and isinstance(f.value, ast.Name) and f.value.id == "generate":
            if not any(isinstance(c, ast.Constant) and c.value == "--check" for a in n.args for c in ast.walk(a)):
                found.append(f"line {n.lineno}: generate.main() in write mode")
        elif f.attr in MUTATORS and _names(f.value) & real:
            found.append(f"line {n.lineno}: .{f.attr}() on a path under the repository root")
        elif f.attr == "open" and _names(f.value) & real:
            mode = _mode(n, 0)
            if mode and WRITE_MODE.search(mode):
                found.append(f"line {n.lineno}: .open({mode!r}) on a path under the repository root")
    return found


class NoCheckoutWritesTest(unittest.TestCase):
    def test_guard_flags_the_old_test_generate(self):
        old = ('ROOT = 1\ndef t():\n    doc = ROOT / "docs/AGENT-API.md"\n    doc.write_text("x")\n'
               '    generate.main([])\n    generate.main(["--check"])\n')
        self.assertEqual(len(violations(old)), 2)
        fixed = old.replace("doc.write_text", "tmp.write_text").replace("main([])", "main(['--check'])")
        self.assertEqual(violations(fixed), [])

    def test_write_bytes_and_unlink_on_the_root_are_flagged(self):
        src = 'ROOT = 1\ntmp = 2\ndef t():\n    (ROOT / "a").write_bytes(b"x")\n    (ROOT / "a").unlink()\n'
        self.assertEqual(len(violations(src)), 2)
        self.assertEqual(violations(src.replace("ROOT /", "tmp /")), [])

    def test_open_for_writing_on_the_root_is_flagged(self):
        writes = ['open(ROOT / "a", "w")', 'open(ROOT / "a", "wb")', 'open(ROOT / "a", "a")',
                  'open(ROOT / "a", "x")', 'open(ROOT / "a", mode="r+")',
                  '(ROOT / "a").open("w")', '(ROOT / "a").open("wb")']
        for call in writes:
            with self.subTest(call=call):
                self.assertEqual(len(violations(f"ROOT = 1\ndef t():\n    {call}\n")), 1)
        reads = ['open(ROOT / "a")', 'open(ROOT / "a", "rb")', '(ROOT / "a").open("r")',
                 'open(tmp / "a", "w")', 'open(ROOT / "a", mode_var)']
        for call in reads:
            with self.subTest(call=call):
                self.assertEqual(violations(f"ROOT = 1\ntmp = 2\ndef t():\n    {call}\n"), [])

    def test_path_from_file_is_flagged_and_the_main_block_is_skipped(self):
        head = 'from pathlib import Path\nHERE = Path(__file__).resolve().parent\n'
        self.assertEqual(len(violations(head + 'def t():\n    (HERE / "x").write_text("")\n')), 1)
        main = head + 'if __name__ == "__main__":\n    (HERE / "x").write_text("")\n'
        self.assertEqual(violations(main), [])

    def test_no_test_modifies_the_checkout(self):
        files = subprocess.run(["git", "ls-files", "*/tests/test_*.py"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.split()
        self.assertTrue(files)
        # Every top-level directory with tests is scanned, not only one of them (a scan of scripts/tests alone passes).
        self.assertTrue({"agent", "tools", "exporters", "scripts"} <= {f.split("/", 1)[0] for f in files})
        bad = {f: v for f in files if (v := violations((ROOT / f).read_text()))}
        self.assertEqual(bad, {}, "tests must work on a temporary copy, never edit tracked files in place")


if __name__ == "__main__":
    unittest.main()
