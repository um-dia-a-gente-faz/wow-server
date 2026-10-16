"""#481: the agent service in dev/docker-compose.yml only sets settings agent/config.py declares,
and its published API port is AGENT_HTTP_PORT.

The dev compose has no generator (unlike docker-compose.agents.yml, #190), so this reads the file
as text: stdlib only, like the rest of agent/tests.
"""
import re
import unittest
from pathlib import Path

from agent.config import SETTINGS

REPO = Path(__file__).resolve().parents[2]
KNOWN = {s.name for s in SETTINGS}


def agent_block(text):
    """The text of the `agent:` service, from its header to the next service header."""
    lines = text.split("\n")
    start = lines.index("  agent:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  \S", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def agent_env_keys(block):
    return set(re.findall(r"^\s+((?:AGENT|JEV)_[A-Z0-9_]+):", block, re.M))


def published_container_ports(block):
    m = re.search(r"^    ports: \[(.*)\]$", block, re.M)
    return [p.split(":")[-1] for p in re.findall(r'"([^"]+)"', m.group(1))] if m else []


class DevComposeAgentTests(unittest.TestCase):
    def setUp(self):
        self.block = agent_block((REPO / "dev" / "docker-compose.yml").read_text())

    def test_agent_env_keys_are_declared_settings(self):
        self.assertEqual(sorted(agent_env_keys(self.block) - KNOWN), [],
                         "dev/docker-compose.yml sets these AGENT_/JEV_ keys but agent/config.py does not declare them")

    def test_http_port_is_the_published_container_port(self):
        m = re.search(r'^\s+AGENT_HTTP_PORT: "(\d+)"', self.block, re.M)
        self.assertIsNotNone(m, "the dev agent service must set AGENT_HTTP_PORT")
        self.assertEqual(published_container_ports(self.block), [m.group(1)])


if __name__ == "__main__":
    unittest.main()
