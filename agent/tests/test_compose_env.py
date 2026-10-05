"""GH #190 / #250: docker-compose.agents.yml passes exactly the settings that
agent/config.py::SETTINGS marks compose=True.

A setting read by agent/ and documented in .env.example is silently dead
unless the compose file passes it through, so drift in either direction fails.
"""
import re
import unittest
from pathlib import Path

from agent.config import SETTINGS

REPO = Path(__file__).resolve().parents[2]

# Passed by compose but not read through SETTINGS.
COMPOSE_ONLY = {
    "LLM_PROVIDER": "passed since UM-44, no code reads it any more",
}


def compose_passed():
    text = (REPO / "docker-compose.agents.yml").read_text()
    text = text.split("\n  jev-mock:")[0]  # the jev-mock service has its own env
    return set(re.findall(r"^\s+([A-Z][A-Z0-9_]+):", text, re.M))


class ComposeEnvPassthroughTests(unittest.TestCase):
    def test_passed_settings_match_schema(self):
        want = {s.name for s in SETTINGS if s.compose} | set(COMPOSE_ONLY)
        got = compose_passed()
        self.assertEqual(sorted(want - got), [], "SETTINGS says compose passes these, the file does not "
                         "(edit scripts/gen_agents_compose.py and regenerate, or set compose=False)")
        self.assertEqual(sorted(got - want), [], "docker-compose.agents.yml passes these but SETTINGS "
                         "does not declare them (declare in agent/config.py or add to COMPOSE_ONLY)")


if __name__ == "__main__":
    unittest.main()
