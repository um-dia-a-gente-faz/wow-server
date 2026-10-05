"""GH #250: one schema (agent/config.py::SETTINGS) for env settings."""
import re
import unittest
from pathlib import Path
from unittest import mock

from agent import config
from agent.config import SETTINGS, Config, ConfigError

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"

# In .env.example but not agent settings (server, monitoring, compose, runner).
ENV_EXAMPLE_OTHER = {
    "MYSQL_ROOT_PASSWORD", "ACCESS_PASSWORD", "TRINITY_DB_PASSWORD", "AGENT_PASSWORD",
    "AGENT_API_URLS", "AGENT_RUNNER_URL", "AGENT_RUNNER_TOKEN", "AGENT_RUNNER_BIND",
    "AGENT_HTTP_PUBLISH_IP",
}
ENV_LINE = re.compile(r"^(#?)\s*([A-Z][A-Z0-9_]+)=(.*)$", re.M)


class NoEnvOutsideConfigTests(unittest.TestCase):
    def test_os_environ_only_in_config(self):
        bad = []
        for path in AGENT.rglob("*.py"):
            rel = path.relative_to(AGENT)
            if "tests" in rel.parts or rel == Path("config.py"):
                continue
            if re.search(r"os\.environ|os\.getenv|\bgetenv\(|from os import[^\n]*environ", path.read_text()):
                bad.append(str(rel))
        self.assertEqual(bad, [], "read settings through agent.config, not os.environ")


class EnvExampleTests(unittest.TestCase):
    def setUp(self):
        self.lines = ENV_LINE.findall((REPO / ".env.example").read_text())
        self.names = {n for _, n, _ in self.lines}

    def test_every_setting_documented(self):
        self.assertEqual(sorted({s.name for s in SETTINGS} - self.names), [])

    def test_no_unknown_names(self):
        known = {s.name for s in SETTINGS} | ENV_EXAMPLE_OTHER
        self.assertEqual(sorted(self.names - known), [], "declare in SETTINGS or ENV_EXAMPLE_OTHER")

    def test_active_values_are_valid(self):
        for comment, name, value in self.lines:
            if comment or name in ENV_EXAMPLE_OTHER:
                continue
            with mock.patch.dict("os.environ", {name: value.split("#")[0]}, clear=True):
                config.get(name)  # raises ConfigError on a bad value


class SchemaTests(unittest.TestCase):
    def test_defaults_have_declared_type(self):
        for s in SETTINGS:
            self.assertIsInstance(s.default, s.kind, s.name)

    def test_invalid_values_fail_with_the_name(self):
        for name, bad in (("WOW_AUTH_PORT", "abc"), ("AGENT_THINK_INTERVAL_S", "fast"),
                          ("VERBOSE_PACKETS", "maybe")):
            with mock.patch.dict("os.environ", {name: bad}, clear=True):
                with self.assertRaises(ConfigError) as cm:
                    Config()
                self.assertIn(name, str(cm.exception))

    def test_empty_numeric_uses_default(self):
        with mock.patch.dict("os.environ", {"AGENT_RUN_DURATION_S": "", "VERBOSE_PACKETS": " "}, clear=True):
            c = Config()
            self.assertEqual(c.run_duration, 0.0)
            self.assertFalse(c.verbose_packets)

    def test_secrets_never_in_redacted_or_repr(self):
        secrets = [s for s in SETTINGS if s.secret]
        env = {s.name: f"sentinel-{s.name}" for s in secrets}
        env["JEV_PROVIDER"] = "openrouter"
        with mock.patch.dict("os.environ", env, clear=True):
            c = Config()
        for text in (str(c.redacted()), repr(c)):
            for s in secrets:
                self.assertNotIn(f"sentinel-{s.name}", text, s.name)


if __name__ == "__main__":
    unittest.main()
