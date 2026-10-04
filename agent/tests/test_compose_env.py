"""GH #190: every env var the agent code reads must reach the containers.

A setting read by agent/ and documented in .env.example is silently dead
unless docker-compose.agents.yml passes it through. This extracts the names
from the call sites and asserts each is in the compose environment block.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"

# Read by agent code but deliberately not passed to the containers.
NOT_PASSED = {
    "WOW_BUILD": "client build, fixed at 12340 for 3.3.5a",
    "WOW_CHAR_GUID": "a global guid would point all 25 agents at one character; each service sets WOW_CHARACTER",
    "AGENT_AUDIT_DIR": "default /data/audit matches the volume mount",
    "AGENT_AUDIT_RETENTION_DAYS": "default is intended",
    "AGENT_CHAT_RELAY_URL": "off by default",
    "AGENT_CHAT_RELAY_TOKEN": "off by default",
    "AGENT_CHAT_RELAY_TIMEOUT_S": "off by default",
    "CHAT_FEED_TOKEN": "used by agent/tools/probe.py, run from a shell",
}

READ = re.compile(
    r"""(?:_env_(?:str|int|float|bool)|os\.environ\.get|os\.getenv)\(\s*["']([A-Z][A-Z0-9_]+)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]""")


def env_names_read():
    names = set()
    for path in AGENT.rglob("*.py"):
        if "tests" in path.relative_to(AGENT).parts:
            continue
        for m in READ.finditer(path.read_text()):
            names.add(m.group(1) or m.group(2))
    return names


def compose_passed():
    text = (REPO / "docker-compose.agents.yml").read_text()
    return set(re.findall(r"^\s+([A-Z][A-Z0-9_]+):", text, re.M))


class ComposeEnvPassthroughTests(unittest.TestCase):
    def test_extractor_finds_known_names(self):
        names = env_names_read()
        for known in ("JEV_API_KEY", "OPENROUTER_API_KEY", "JEV_MIN_CONFIDENCE", "AGENT_AUDIT_DIR"):
            self.assertIn(known, names)

    def test_every_env_var_read_is_passed_or_allowlisted(self):
        missing = sorted(env_names_read() - compose_passed() - set(NOT_PASSED))
        self.assertEqual(missing, [], "read by agent/ but not passed by docker-compose.agents.yml "
                         "(add to scripts/gen_agents_compose.py, or to NOT_PASSED with a reason)")

    def test_allowlist_has_no_stale_entries(self):
        stale = sorted((set(NOT_PASSED) - env_names_read()) | (set(NOT_PASSED) & compose_passed()))
        self.assertEqual(stale, [], "NOT_PASSED entries that are no longer read or are now passed")


if __name__ == "__main__":
    unittest.main()
