"""Ratchet for mypy.ini (#350, #380): the legacy tier may shrink, never grow.

A module under `ignore_errors = True` must be in BASELINE. To move a module up a tier,
delete its [mypy-...] section and its line here, in the same PR. A new agent/*.py file gets
no section (it is `checked` by default) and must not be added to BASELINE.

The checkers take ini text, so the tests can feed them synthetic input (#380).
"""
import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BASELINE = {
    "agent.actions.chat",
    "agent.actions.combat",
    "agent.actions.loot",
    "agent.actions.mail",
    "agent.actions.movement",
    "agent.actions.quest",
    "agent.actions.trade",
    "agent.actions.vendor",
    "agent.chat_relay",
    "agent.control",
    "agent.handles",
    "agent.http_api",
    "agent.item_compare",
    "agent.jev",
    "agent.loot",
    "agent.metrics",
    "agent.names",
    "agent.npc",
    "agent.quests",
    "agent.reflexes.follow",
    "agent.session",
    "agent.spells",
    "agent.state",
    "agent.transport",
    "agent.update_fields",
    # tools/ (#379); wowmap modules are top-level names
    "dbc.names",
    "dbc.spelltext",
    "loadtest.soak",
    "activity",
    "app",
    "calibrate",
    "extract_icons",
    "extract_maps",
    "fit_transform",
    "fog",
    "models",
    "overlays",
    "state",
    "transform",
}

# #380: settings that switch the ratchet off, pinned at their values on main.
PINNED_EXCLUDE = r"^(agent|tools/[^/]+)/tests/|tools/wowmap/[a-z_]*_test\.py"
# follow_imports = skip/silent hides imported modules from the check. Only the global
# `silent` that is already in mypy.ini may stay; any other one is a bypass.
PINNED_FOLLOW_IMPORTS = {("mypy", "silent")}


def load(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return cp


def read_mypy_ini():
    return load((ROOT / "mypy.ini").read_text())


def _flag(cp, section, name):
    """Boolean option `name` in `section`; mypy accepts dashes or underscores in keys."""
    for key, value in cp[section].items():
        if key.replace("-", "_") == name:
            return cp.BOOLEAN_STATES.get(value.strip().lower(), False)
    return False


def legacy_sections(cp):
    return {s[len("mypy-"):] for s in cp.sections()
            if s.startswith("mypy-") and _flag(cp, s, "ignore_errors")}


def stale_baseline(cp, baseline=BASELINE):
    """BASELINE entries with no legacy section left in mypy.ini (promoted, renamed or gone)."""
    return baseline - legacy_sections(cp)


def bypass_problems(cp):
    """Settings that would let a module skip the check without a tier entry."""
    problems = []
    if _flag(cp, "mypy", "ignore_errors"):
        problems.append("[mypy] ignore_errors = True switches the whole check off")
    exclude = cp.get("mypy", "exclude", fallback=None)
    if exclude is None or exclude.strip() != PINNED_EXCLUDE:
        problems.append(f"[mypy] exclude changed (pinned: {PINNED_EXCLUDE!r}, got {exclude!r})")
    for section in cp.sections():
        for key, value in cp[section].items():
            if key.replace("-", "_") != "follow_imports":
                continue
            pair = (section, value.strip().lower())
            if pair[1] in {"skip", "silent"} and pair not in PINNED_FOLLOW_IMPORTS:
                problems.append(f"[{section}] follow_imports = {value.strip()} hides imports")
    return problems


def agent_modules():
    return {".".join(p.relative_to(ROOT).with_suffix("").parts)
            for p in (ROOT / "agent").rglob("*.py") if "tests" not in p.parts}


def tools_modules():
    """Names as mypy sees them: tools/wowmap is a base dir, other tools/<pkg> are packages."""
    out = set()
    for p in (ROOT / "tools").rglob("*.py"):
        parts = p.relative_to(ROOT / "tools").with_suffix("").parts
        if "tests" in parts or "node_modules" in parts:
            continue
        out.add(".".join(parts[1:] if parts[0] == "wowmap" else parts))
    return out


class MypyRatchet(unittest.TestCase):
    def test_legacy_never_grows(self):
        new = legacy_sections(read_mypy_ini()) - BASELINE
        self.assertFalse(new, f"new modules must type-check, not join legacy: {sorted(new)}")

    def test_legacy_entries_exist(self):
        mods = agent_modules() | tools_modules()
        for pat in legacy_sections(read_mypy_ini()):
            prefix = pat[:-2] if pat.endswith(".*") else pat
            ok = any(m == prefix or m.startswith(prefix + ".") for m in mods)
            self.assertTrue(ok, f"mypy.ini legacy entry {pat} matches no module; delete it")

    def test_every_baseline_entry_is_in_mypy_ini(self):
        stale = stale_baseline(read_mypy_ini())
        self.assertFalse(stale, "BASELINE entries with no ignore_errors section in mypy.ini "
                                f"(promoted or removed; delete them from BASELINE): {sorted(stale)}")

    def test_no_global_bypass(self):
        self.assertEqual(bypass_problems(read_mypy_ini()), [])

    def test_whole_agent_is_checked(self):
        cp = read_mypy_ini()
        files = {f.strip() for f in cp["mypy"]["files"].split(",")}
        self.assertTrue({"agent", "tools/common", "tools/dbc", "tools/wowmap"} <= files)


REAL_INI = (ROOT / "mypy.ini").read_text()


def mutate(old, new):
    """The real mypy.ini with one line swapped; fails loudly if `old` no longer exists."""
    assert old in REAL_INI, f"mutation anchor gone from mypy.ini: {old!r}"
    return REAL_INI.replace(old, new, 1)


class RatchetCatchesBypass(unittest.TestCase):
    """Each bad edit to mypy.ini must be reported by the checkers (#380 mutation proof)."""

    def test_promoted_module_still_in_baseline_is_reported(self):
        cp = load(mutate("[mypy-agent.jev]\nignore_errors = True\n", ""))
        self.assertEqual(stale_baseline(cp), {"agent.jev"})

    def test_renamed_legacy_section_is_reported(self):
        cp = load(mutate("[mypy-agent.jev]\n", "[mypy-agent.jevx]\n"))
        self.assertIn("agent.jev", stale_baseline(cp))

    def test_real_ini_has_no_stale_baseline(self):
        self.assertEqual(stale_baseline(load(REAL_INI)), set())

    def test_global_ignore_errors_is_reported(self):
        for line in ("ignore_errors = True", "ignore-errors = yes"):
            with self.subTest(line=line):
                cp = load(mutate("warn_unused_ignores = True",
                                 f"warn_unused_ignores = True\n{line}"))
                self.assertTrue(bypass_problems(cp))
                # the per-module tier is untouched: only the bypass check sees this one
                self.assertEqual(legacy_sections(cp), legacy_sections(load(REAL_INI)))

    def test_exclude_change_is_reported(self):
        cp = load(mutate(PINNED_EXCLUDE, "^(agent|tools/[^/]+)/tests/"))
        self.assertTrue(any("exclude" in p for p in bypass_problems(cp)))

    def test_global_follow_imports_skip_is_reported(self):
        cp = load(mutate("follow_imports = silent", "follow_imports = skip"))
        self.assertTrue(any("follow_imports" in p for p in bypass_problems(cp)))

    def test_per_module_follow_imports_silent_is_reported(self):
        cp = load(REAL_INI + "\n[mypy-agent.fresh]\nfollow_imports = silent\n")
        self.assertTrue(any("follow_imports" in p for p in bypass_problems(cp)))

    def test_pinned_state_is_clean(self):
        self.assertEqual(bypass_problems(load(REAL_INI)), [])


if __name__ == "__main__":
    unittest.main()
