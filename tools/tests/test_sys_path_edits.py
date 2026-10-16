"""The count of sys.path edits only goes down (ADR 0019, #347 slice 1). Stdlib only.

The tree is walked (no git), and every line that edits sys.path is counted, in two classes:
production (no path component is `tests`) and tests. Each class has one constant, the raw count.
A count above its constant fails: a new sys.path edit is imported from the checkout root instead;
a deliberate one (a `-I` subprocess) raises the constant in the same PR, with the reason in a
comment, and is named in ALLOWED. A count below its constant fails too, so the constant is lowered
as soon as an edit goes. ALLOWED is not subtracted from any count; it is checked for staleness.
"""
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WALK = ("agent", "agents", "scripts", "tools", "monitoring", "exporters", "docs")
# ADR 0019, Inventory, pattern P. The source of this file holds no line that P matches.
PATTERN = re.compile(r"sys\.path(\.insert|\.append|\.pop|\.extend|\[[^]]*\] *=| *\+=| *=[^=])|site\.addsitedir")

PRODUCTION_EDITS = 24
TEST_EDITS = 77
# Edits that stay on purpose. Named here, counted in the constants above.
ALLOWED = {"scripts/early_failures.py"}
# The root insert removed from each test file (ADR 0019, first slice). Built from parts so this file
# has no literal matching line. The files keep their other edits (the tools/ insert, HERE.parent,
# the subprocess script text), so the check is for the removed line, not for "no edit at all".
_SYS_PATH = "sys" + ".path"
NO_EDITS = {
    "tools/tests/test_audit_reader_keys.py": _SYS_PATH + ".insert(0, str(ROOT))",
    "tools/tests/test_roster_accounts.py": _SYS_PATH + ".insert(0, str(ROOT))",
    "tools/agent-runner/tests/test_runner.py": _SYS_PATH + ".insert(0, str(REPO))",
    "tools/worldknowledge/tests/test_build.py": _SYS_PATH + ".insert(0, str(ROOT))",
}


def find_edits(root):
    """(relative path, is_test, line text) for every matching line under WALK."""
    root = pathlib.Path(root)
    for top in WALK:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root)
            if "__pycache__" in rel.parts:
                continue
            is_test = "tests" in rel.parts
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if PATTERN.search(line):
                    yield rel.as_posix(), is_test, line


def count_edits(root):
    counts = {"production": 0, "tests": 0}
    for _rel, is_test, _line in find_edits(root):
        counts["tests" if is_test else "production"] += 1
    return counts


def check_counts(counts):
    """Failure messages for counts against the constants: over fails, under fails."""
    failures = []
    for name, limit in (("production", PRODUCTION_EDITS), ("tests", TEST_EDITS)):
        n = counts[name]
        if n > limit:
            failures.append(f"{name}: {n} sys.path edits, above the constant {limit}: a new sys.path edit; "
                            "import from the checkout root instead (ADR 0019). A legitimate edit (a `-I` "
                            "subprocess, rule 3): raise the constant with the reason in a comment, and name "
                            "the file in `ALLOWED`")
        elif n < limit:
            failures.append(f"{name}: {n} sys.path edits, below the constant {limit}: lower the constant to {n}")
    return failures


def check_no_edits(root):
    """A removed root insert must not come back in its file."""
    root = pathlib.Path(root)
    return [f"{rel}: the removed `{fragment}` is back (ADR 0019)"
            for rel, fragment in sorted(NO_EDITS.items())
            if (root / rel).is_file() and fragment in (root / rel).read_text(encoding="utf-8")]


def check_allowed(root, allowed):
    """An ALLOWED path must exist and still hold a matching line."""
    root = pathlib.Path(root)
    failures = []
    hit = {rel for rel, _is_test, _line in find_edits(root)}
    for rel in sorted(allowed):
        if not (root / rel).is_file():
            failures.append(f"{rel}: in ALLOWED but missing")
        elif rel not in hit:
            failures.append(f"{rel}: in ALLOWED but has no sys.path edit any more")
    return failures


def _write(root, rel, lines):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


# Built from parts: this file must not contain a literal line that P matches.
FIXTURE_EDIT = "sys." + "path.insert(0, x)\n"


class SysPathRatchetTests(unittest.TestCase):
    def test_repo_counts_match_the_constants(self):
        self.assertEqual(check_counts(count_edits(ROOT)), [])

    def test_a_new_edit_in_production_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "tools/fixture.py", [FIXTURE_EDIT] * (PRODUCTION_EDITS + 1))
            failures = check_counts(count_edits(tmp))
        self.assertEqual(len(failures), 2)   # the production over-count, and the tests class (0) under its constant
        self.assertIn("above the constant", failures[0])
        self.assertIn("raise the constant with the reason in a comment, and name the file in `ALLOWED`", failures[0])

    def test_a_new_edit_in_tests_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "tools/tests/test_fixture.py", [FIXTURE_EDIT] * (TEST_EDITS + 1))
            failures = [f for f in check_counts(count_edits(tmp)) if f.startswith("tests:")]
        self.assertEqual(len(failures), 1)
        self.assertIn("above the constant", failures[0])

    def test_one_below_the_constant_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "tools/fixture.py", [FIXTURE_EDIT] * (PRODUCTION_EDITS - 1))
            _write(tmp, "tools/tests/test_fixture.py", [FIXTURE_EDIT] * (TEST_EDITS - 1))
            failures = check_counts(count_edits(tmp))
        self.assertEqual(len(failures), 2)
        self.assertTrue(all("below the constant" in f and "lower the constant" in f for f in failures))

    def test_the_four_removed_root_inserts_stay_removed(self):
        self.assertEqual(check_no_edits(ROOT), [])

    def test_no_edits_catches_a_returned_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "tools/tests/test_roster_accounts.py", [FIXTURE_EDIT.replace("x", "str(ROOT)")])
            failures = check_no_edits(tmp)
        self.assertEqual(len(failures), 1)

    def test_allowed_is_checked_for_staleness_only(self):
        self.assertEqual(check_allowed(ROOT, ALLOWED), [])
        self.assertIn("scripts/early_failures.py", {rel for rel, _t, _l in find_edits(ROOT)})

    def test_allowed_fails_when_missing_or_no_longer_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "tools/clean.py", ["x = 1\n"])
            failures = check_allowed(tmp, {"scripts/gone.py", "tools/clean.py"})
        self.assertEqual(len(failures), 2)
        self.assertIn("missing", failures[0])
        self.assertIn("no sys.path edit", failures[1])

    def test_the_source_of_this_file_has_no_matching_line(self):
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        self.assertEqual([l for l in source.splitlines() if PATTERN.search(l)], [])


if __name__ == "__main__":
    unittest.main()
