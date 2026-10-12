"""agents/compose.py: the compose renderer as a library (#391). The script keeps the command line.

Runs from the repo root (`discover -t . -s agent/tests`), so `agents` imports as a namespace package."""
import ast
import sys
import unittest
from pathlib import Path

from agents import compose, roster

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import gen_agents_compose as gen  # noqa: E402


def entries():
    return [a.to_json() for a in roster.load_roster()]


class ComposeLibraryTests(unittest.TestCase):
    def test_render_is_the_committed_file(self):
        self.assertEqual(compose.render(entries()), (REPO / "docker-compose.agents.yml").read_text())

    def test_the_script_renders_with_the_library(self):
        for name in ("render", "derive", "HEADER", "SERVICE", "JEV_MOCK", "DEFAULT_AUDIT_DIR", "RAID_SIZE"):
            self.assertIs(getattr(gen, name), getattr(compose, name), name)

    def test_the_script_defines_no_template_of_its_own(self):
        source = (REPO / "scripts" / "gen_agents_compose.py").read_text()
        for marker in ("x-agent:", "container_name:", "jev-mock:", "def render", "def derive"):
            self.assertNotIn(marker, source, marker)

    def test_audit_dir_replaces_only_the_volume_line(self):
        default, other = compose.render(entries()), compose.render(entries(), audit_dir="/srv/audit")
        self.assertIn("    - /srv/audit:/data/audit\n", other)
        self.assertEqual(other.replace("- /srv/audit:/data/audit", "- /opt/wow-server-metrics/audit:/data/audit"),
                         default)

    def test_derive_reads_only_the_five_keys(self):
        agent = compose.derive(25, {"account": "AGENT26", "character": "Delta", "retired": True, "level": 1})
        self.assertEqual((agent.index, agent.api_port, agent.service, agent.container, agent.profiles),
                         (26, roster.FIRST_PORT + 25, "agent-delta", "wow-agent-delta", ("raid",)))

    def test_the_library_imports_only_the_roster_library(self):
        tree = ast.parse((REPO / "agents" / "compose.py").read_text())
        imports = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, ["from . import roster"])


if __name__ == "__main__":
    unittest.main()
