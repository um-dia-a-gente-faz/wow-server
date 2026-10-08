"""Ratchet for mypy.ini (#350): the legacy tier may shrink, never grow.

A module under `ignore_errors = True` must be in BASELINE. To move a module up a tier,
delete its [mypy-...] section and its line here. A new agent/*.py file gets no section
(it is `checked` by default) and must not be added to BASELINE.
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
    "agent.perception.fields",
    "agent.perception.objects",
    "agent.perception.resolution",
    "agent.perception.trade_state",
    "agent.perception.windows",
    "agent.perception.world",
    "agent.quests",
    "agent.reflexes.follow",
    "agent.session",
    "agent.spells",
    "agent.state",
    "agent.tools.ab",
    "agent.tools.cache_probe",
    "agent.tools.dump_update",
    "agent.tools.probe",
    "agent.transport",
    "agent.update_fields",
}


def legacy_sections():
    cp = configparser.ConfigParser()
    cp.read(ROOT / "mypy.ini")
    return {s[len("mypy-"):] for s in cp.sections()
            if s.startswith("mypy-") and cp.getboolean(s, "ignore_errors", fallback=False)}


def agent_modules():
    return {".".join(p.relative_to(ROOT).with_suffix("").parts)
            for p in (ROOT / "agent").rglob("*.py") if "tests" not in p.parts}


class MypyRatchet(unittest.TestCase):
    def test_legacy_never_grows(self):
        new = legacy_sections() - BASELINE
        self.assertFalse(new, f"new modules must type-check, not join legacy: {sorted(new)}")

    def test_legacy_entries_exist(self):
        mods = agent_modules()
        for pat in legacy_sections():
            prefix = pat[:-2] if pat.endswith(".*") else pat
            ok = any(m == prefix or m.startswith(prefix + ".") for m in mods)
            self.assertTrue(ok, f"mypy.ini legacy entry {pat} matches no module; delete it")

    def test_whole_agent_is_checked(self):
        cp = configparser.ConfigParser()
        cp.read(ROOT / "mypy.ini")
        self.assertEqual(cp["mypy"]["files"].strip(), "agent")


if __name__ == "__main__":
    unittest.main()
