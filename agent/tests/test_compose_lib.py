"""agents/compose.py: the compose renderer as a library (#391). The script keeps the command line.

Runs from the repo root (`discover -t . -s agent/tests`), so `agents` imports as a namespace package."""
import ast
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent.config import Config
from agents import compose, roster

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:  # stays for the process, by design: the module imports from scripts/
    sys.path.insert(0, str(REPO / "scripts"))
import gen_agents_compose as gen  # noqa: E402


def entries():
    return [a.to_json() for a in roster.load_roster()]


class ComposeLibraryTests(unittest.TestCase):
    def test_render_is_the_committed_file(self):
        self.assertEqual(compose.render(entries()), (REPO / "docker-compose.agents.yml").read_text())

    def test_state_dir_is_the_mounted_directory_and_each_agent_gets_its_slug(self):
        text = compose.render(entries(), state_dir="/r/state")
        self.assertIn("- /r/state:/data/state\n", text)
        self.assertNotIn("/opt/wow-server-metrics/agent-state", text)
        self.assertIn("AGENT_STATE_DIR: /data/state/luaprata\n", text)
        self.assertEqual(len(re.findall(r"AGENT_STATE_DIR: /data/state/", text)), 25)

    def test_start_delay_is_three_seconds_per_roster_position_modulo_sixty(self):
        text = compose.render([{"account": f"A{i}", "character": f"Name{'abcdefghijklmnopqrstuvwxyz'[i]}",
                                "retired": i == 2} for i in range(26)])
        delays = [int(m) for m in re.findall(r'AGENT_START_DELAY_S: "(\d+)"', text)]
        # retired entries are rendered like any other, so they count in the position
        self.assertEqual(len(delays), 26)
        self.assertEqual([delays[i] for i in (0, 1, 19, 20, 25)], [0, 3, 57, 0, 15])
        self.assertEqual(delays[:4], [0, 3, 6, 9])

    def test_the_script_renders_with_the_library(self):
        for name in ("render", "derive", "HEADER", "SERVICE", "JEV_MOCK", "DEFAULT_AUDIT_DIR", "RAID_SIZE"):
            self.assertIs(getattr(gen, name), getattr(compose, name), name)

    def test_the_script_defines_no_template_of_its_own(self):
        source = (REPO / "scripts" / "gen_agents_compose.py").read_text()
        for marker in ("x-agent:", "container_name:", "jev-mock:", "def render", "def derive"):
            self.assertNotIn(marker, source, marker)

    def test_agents_get_the_pathfinder_url_in_the_shared_env(self):
        # #816: the sidecar runs on the wow-server VM (monitoring/docker-compose.yml), published on 8770
        text = compose.render(entries())
        self.assertEqual(text.count("AGENT_PATHFINDER_URL:"), 1)   # one anchor, every service merges it
        env = text.split("environment: &env-defaults\n", 1)[1].split("  # Decision audit log", 1)[0]
        self.assertIn("    AGENT_PATHFINDER_URL: http://192.168.1.64:8770\n", env)

    def test_audit_dir_replaces_only_the_volume_line(self):
        default, other = compose.render(entries()), compose.render(entries(), audit_dir="/srv/audit")
        self.assertIn("    - /srv/audit:/data/audit\n", other)
        self.assertEqual(other.replace("- /srv/audit:/data/audit", "- /opt/wow-server-metrics/audit:/data/audit"),
                         default)

    # ── #747: the fleet limits directory (ADR 0017 further slice 1) ──────────
    def _split(self, text):
        """(x-agent block, {service: block}) of a rendered file, by text: no YAML library in the repo."""
        head, rest = text.split("\nservices:\n", 1)
        blocks = re.split(r"(?m)^  (?=[a-z][a-z-]*:$)", rest)
        return head, {b.split(":", 1)[0]: b for b in blocks if b.strip() and not b.startswith("#")}

    def test_every_agent_service_gets_the_fleet_directory_read_only_and_jev_mock_does_not(self):
        head, services = self._split(compose.render(entries()))
        # the directory, never the file (ADR 0017: a file bind mount pins the inode), read-only
        self.assertIn("    - /opt/wow-server-metrics/fleet:/data/fleet:ro\n", head)
        self.assertEqual(len(re.findall(r"(?m)^    - .*:/data/fleet", head)), 1)
        self.assertEqual(len(services) - 1, len(entries()))
        for name, block in services.items():
            if name == "jev-mock":
                self.assertNotIn("fleet", block)
            else:   # inherits x-agent's volumes: no service may replace that list
                self.assertIn("<<: *agent-defaults", block, name)
                self.assertNotIn("\n    volumes:", block, name)
                # the value comes from the host variable through x-agent's environment, never per service
                self.assertIn("      <<: *env-defaults\n", block, name)
                self.assertNotIn("AGENT_FLEET_LIMITS_FILE", block, name)

    def test_fleet_dir_replaces_only_its_volume_line(self):
        default, other = compose.render(entries()), compose.render(entries(), fleet_dir="/srv/fleet")
        self.assertIn("    - /srv/fleet:/data/fleet:ro\n", other)
        self.assertEqual(other.replace("- /srv/fleet:/data/fleet:ro", "- /opt/wow-server-metrics/fleet:/data/fleet:ro"),
                         default)

    def test_the_limits_file_setting_is_passed_through_empty_by_default(self):
        head, _ = self._split(compose.render(entries()))
        self.assertEqual(re.findall(r"(?m)^    AGENT_FLEET_LIMITS_FILE: (.*)$", head), ["${AGENT_FLEET_LIMITS_FILE:-}"])

    def test_the_rendered_default_reaches_config_as_the_empty_string(self):
        head, _ = self._split(compose.render(entries()))
        value = re.search(r"(?m)^    AGENT_FLEET_LIMITS_FILE: \$\{AGENT_FLEET_LIMITS_FILE:-(.*)\}$", head).group(1)
        self.assertEqual(value, "")   # compose substitutes the default when the host variable is unset or empty
        with mock.patch.dict("os.environ", {"AGENT_FLEET_LIMITS_FILE": value}, clear=True):
            self.assertEqual(Config().fleet_limits_file, "")
        with mock.patch.dict("os.environ", {"AGENT_FLEET_LIMITS_FILE": "/data/fleet/limits.json"}, clear=True):
            self.assertEqual(Config().fleet_limits_file, "/data/fleet/limits.json")

    # ── #749: the world bundle directory (ADR 0016 further slice 2) ──────────
    def test_every_agent_service_gets_the_world_directory_read_only_and_jev_mock_does_not(self):
        head, services = self._split(compose.render(entries()))
        # the directory, never the file (as ADR 0017: the build replaces the file by rename), read-only
        self.assertIn("    - /opt/wow-server-metrics/world:/data/world:ro\n", head)
        self.assertEqual(len(re.findall(r"(?m)^    - .*:/data/world", head)), 1)
        self.assertEqual(len(services) - 1, len(entries()))
        for name, block in services.items():
            if name == "jev-mock":
                self.assertNotIn("world", block)
            else:   # inherits x-agent's volumes: no service may replace that list
                self.assertIn("<<: *agent-defaults", block, name)
                self.assertNotIn("\n    volumes:", block, name)
                self.assertIn("      <<: *env-defaults\n", block, name)
                self.assertNotIn("AGENT_WORLD_BUNDLE", block, name)

    def test_world_dir_replaces_only_its_volume_line(self):
        default, other = compose.render(entries()), compose.render(entries(), world_dir="/srv/world")
        self.assertIn("    - /srv/world:/data/world:ro\n", other)
        self.assertEqual(other.replace("- /srv/world:/data/world:ro", "- /opt/wow-server-metrics/world:/data/world:ro"),
                         default)

    def test_the_world_bundle_setting_is_passed_through_empty_by_default(self):
        head, _ = self._split(compose.render(entries()))
        self.assertEqual(re.findall(r"(?m)^    AGENT_WORLD_BUNDLE: (.*)$", head), ["${AGENT_WORLD_BUNDLE:-}"])

    def test_the_rendered_world_default_reaches_config_as_the_empty_string(self):
        head, _ = self._split(compose.render(entries()))
        value = re.search(r"(?m)^    AGENT_WORLD_BUNDLE: \$\{AGENT_WORLD_BUNDLE:-(.*)\}$", head).group(1)
        self.assertEqual(value, "")   # off: no reader is built, get_spell_info is unchanged
        with mock.patch.dict("os.environ", {"AGENT_WORLD_BUNDLE": value}, clear=True):
            self.assertEqual(Config().world_bundle, "")
        with mock.patch.dict("os.environ", {"AGENT_WORLD_BUNDLE": "/data/world/world.sqlite3"}, clear=True):
            self.assertEqual(Config().world_bundle, "/data/world/world.sqlite3")

    # ── #509: AGENT_ROSTER, the names of the whole roster, on every agent service (agent/roster in config) ──
    def test_every_agent_service_gets_the_whole_roster_names_in_roster_order(self):
        _, services = self._split(compose.render(entries()))
        names = ",".join(a["character"] for a in entries())
        agent_blocks = {k: v for k, v in services.items() if k != "jev-mock"}
        self.assertEqual(len(agent_blocks), len(entries()))
        for name, block in agent_blocks.items():
            self.assertEqual(re.findall(r'(?m)^      AGENT_ROSTER: "(.*)"$', block), [names], name)

    def test_a_two_agent_roster_names_both_agents_on_both_services(self):
        two = [{"account": "AGENT01", "character": "Luaprata", "race": None, "class": None, "gender": None},
               {"account": "AGENT02", "character": "Farstrider", "race": None, "class": None, "gender": None}]
        _, services = self._split(compose.render(two))
        self.assertEqual(sorted(services), ["agent-farstrider", "agent-luaprata", "jev-mock"])
        for name in ("agent-luaprata", "agent-farstrider"):
            self.assertEqual(re.findall(r'(?m)^      AGENT_ROSTER: "(.*)"$', services[name]), ["Luaprata,Farstrider"], name)

    def test_retired_entries_stay_in_the_roster_names(self):   # the runner renders them too (#509)
        rows = [{"account": "AGENT01", "character": "Luaprata", "retired": True},
                {"account": "AGENT02", "character": "Farstrider"}]
        _, services = self._split(compose.render(rows))
        self.assertEqual(re.findall(r'(?m)^      AGENT_ROSTER: "(.*)"$', services["agent-farstrider"]), ["Luaprata,Farstrider"])

    # ── #314 / #762: AGENT_MISSION, each service's own roster mission (agent/config compose=False) ──
    def test_every_agent_service_gets_its_own_roster_mission(self):
        _, services = self._split(compose.render(entries()))
        agent_blocks = {k: v for k, v in services.items() if k != "jev-mock"}
        self.assertEqual(len(agent_blocks), len(entries()))
        for name, block in agent_blocks.items():   # the committed roster has no mission: all level_to_cap
            self.assertEqual(re.findall(r'(?m)^      AGENT_MISSION: (.*)$', block), ["level_to_cap"], name)

    def test_a_two_agent_roster_gives_each_service_its_own_mission(self):
        two = [{"account": "AGENT01", "character": "Luaprata", "mission": "explore"},
               {"account": "AGENT02", "character": "Farstrider"}]
        _, services = self._split(compose.render(two))
        self.assertEqual(re.findall(r'(?m)^      AGENT_MISSION: (.*)$', services["agent-luaprata"]), ["explore"])
        self.assertEqual(re.findall(r'(?m)^      AGENT_MISSION: (.*)$', services["agent-farstrider"]), ["level_to_cap"])
        self.assertNotIn("AGENT_MISSION", services["jev-mock"])

    def test_derive_reads_only_the_known_keys(self):
        agent = compose.derive(25, {"account": "AGENT26", "character": "Delta", "retired": True, "level": 1})
        self.assertEqual((agent.index, agent.api_port, agent.service, agent.container, agent.profiles),
                         (26, roster.FIRST_PORT + 25, "agent-delta", "wow-agent-delta", ("raid",)))

    def test_the_library_imports_only_the_roster_library(self):
        tree = ast.parse((REPO / "agents" / "compose.py").read_text())
        imports = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, ["from . import roster"])


if __name__ == "__main__":
    unittest.main()
