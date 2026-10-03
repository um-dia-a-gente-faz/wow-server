"""GH #132: scripts/gen_agent_api_urls.py must agree with the committed
docker-compose.agents.yml (names and ports) and with wowmap's parser."""
import contextlib
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import gen_agent_api_urls as urls  # noqa: E402
import gen_agents_compose as gen   # noqa: E402

HOST = "agents.test"


def compose_agents():
    """[(WOW_CHARACTER, AGENT_HTTP_PORT)] per agent service in the committed file."""
    text = (REPO / "docker-compose.agents.yml").read_text()
    chars = re.findall(r"^      WOW_CHARACTER: (\S+)$", text, re.M)
    ports = re.findall(r"^      AGENT_HTTP_PORT: (\d+)$", text, re.M)
    return list(zip(chars, map(int, ports)))


def parse_like_wowmap(spec):
    """Same rules as tools/wowmap/app.py parse_agent_urls (that module needs
    pymysql, which agent/ tests must not): split on ',', then on the first '='."""
    out = {}
    for part in spec.split(","):
        name, sep, url = part.strip().partition("=")
        if sep and name and url.startswith(("http://", "https://")):
            out[name.lower()] = (name, url)
    return out


class AgentApiUrlsTests(unittest.TestCase):
    def test_matches_the_committed_compose_file(self):
        want = ",".join(f"{c}=http://{HOST}:{p}" for c, p in compose_agents())
        self.assertEqual(len(compose_agents()), len(gen.load_roster()))
        self.assertEqual(urls.render(gen.load_roster(), HOST), want)

    def test_roster_order_and_port_base(self):
        out = urls.render(gen.load_roster(), HOST).split(",")
        self.assertEqual(out[0], "Luaprata=http://agents.test:9601")
        self.assertEqual(out[4], "Spellweaver=http://agents.test:9605")
        self.assertEqual(len(out), 25)

    def test_every_entry_survives_the_wowmap_parsing_rules(self):
        parsed = parse_like_wowmap(urls.render(gen.load_roster(), HOST))
        self.assertEqual(len(parsed), 25)
        self.assertEqual(parsed["luaprata"], ("Luaprata", "http://agents.test:9601"))

    def test_bad_hosts_are_rejected(self):
        for bad in ("", "http://h", "h:9601", "a,b", "a=b", "a b", "h/", "-h"):
            with self.assertRaises(ValueError, msg=bad):
                urls.render(gen.load_roster(), bad)

    def test_host_is_required(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_HOST"}
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
            urls.main([])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("agent host is required", err.getvalue())

    def test_host_flag_prints_the_env_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(urls.main(["--host", HOST]), 0)
        line = out.getvalue()
        self.assertTrue(line.startswith("AGENT_API_URLS=Luaprata=http://agents.test:9601,"), line)
        self.assertEqual(line.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
