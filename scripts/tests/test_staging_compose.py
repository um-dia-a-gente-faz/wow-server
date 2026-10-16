"""#400: staging/docker-compose.yml shares nothing with docker-compose.yml that two stacks could
collide on (container names, host ports, named volumes, networks), and the services both files
define use identical image pins. Read as text (no yaml dependency), so this stays stdlib-only.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROD = ROOT / "docker-compose.yml"
STAGING = ROOT / "staging" / "docker-compose.yml"
SHARED_SERVICES = ("trinitycore-wowserver", "database")
STAGING_HOST_PORTS = {3725, 8086, 3307}  # auth, world, db (127.0.0.1 only)

PORT = re.compile(r'^\s+- "(?:([\d.]+):)?(\d+):(\d+)"', re.M)
ENTRY = re.compile(r"^  ([\w.-]+):[ \t]*(?:\{\})?[ \t]*\n((?:    .*\n)*)", re.M)


def top_block(text, key):
    """The indented lines under the top-level `key:` (up to the next column-0 line)."""
    m = re.search(rf"^{key}:\n((?:(?:[ \t].*)?\n)*)", text, re.M)
    return m.group(1) if m else ""


def service_block(text, name):
    m = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*|[ \t]*)\n)*)", top_block(text, "services"), re.M)
    return m.group(1) if m else ""


def service_names(text):
    return set(re.findall(r"^  ([\w.-]+):\s*\n", top_block(text, "services"), re.M))


def field(block, key):
    m = re.search(rf"^    {key}:[ \t]*(\S+)", block, re.M)
    return m.group(1) if m else None


def resources(text, key):
    """Each top-level volume or network: its explicit `name:`, else its key."""
    out = {}
    for m in ENTRY.finditer(top_block(text, key)):
        explicit = re.search(r"^    name:\s*(\S+)", m.group(2), re.M)
        out[m.group(1)] = explicit.group(1) if explicit else m.group(1)
    return out


def facts(text):
    services = service_names(text)
    return {
        "containers": {field(service_block(text, s), "container_name") for s in services} - {None},
        "host_ports": {int(m.group(2)) for m in PORT.finditer(text)},
        "volumes": set(resources(text, "volumes").values()),
        "networks": set(resources(text, "networks").values()),
    }


def collisions(prod_text, staging_text):
    """What staging shares with production, as `kind: value` strings; [] when it shares nothing."""
    p, s = facts(prod_text), facts(staging_text)
    return [f"{kind}: {value}" for kind in ("containers", "host_ports", "volumes", "networks")
            for value in sorted(map(str, p[kind] & s[kind]))]


class StagingComposeTest(unittest.TestCase):
    def setUp(self):
        self.prod = PROD.read_text(encoding="utf-8")
        self.staging = STAGING.read_text(encoding="utf-8")

    def test_shares_no_container_port_volume_or_network(self):
        self.assertEqual(collisions(self.prod, self.staging), [])

    def test_a_production_port_in_staging_is_a_collision(self):
        # the check catches what it claims to: the game VM's world port copied into staging
        leaked = self.staging.replace('"8086:8086"', '"8085:8085"')
        self.assertNotEqual(leaked, self.staging, "fixture did not change the staging file")
        self.assertIn("host_ports: 8085", collisions(self.prod, leaked))

    def test_shared_services_use_identical_images(self):
        for name in SHARED_SERVICES:
            prod_image = field(service_block(self.prod, name), "image")
            staging_image = field(service_block(self.staging, name), "image")
            self.assertIsNotNone(prod_image, f"{name} missing from docker-compose.yml")
            self.assertEqual(staging_image, prod_image, name)

    def test_runs_only_the_worldserver_and_database(self):
        self.assertEqual(service_names(self.staging), set(SHARED_SERVICES))

    def test_publishes_only_the_staging_ports_db_on_loopback(self):
        self.assertEqual(facts(self.staging)["host_ports"], STAGING_HOST_PORTS)
        self.assertIn('"127.0.0.1:3307:3306"', self.staging)

    def test_no_bind_mount_of_the_game_stack_paths(self):
        self.assertNotIn("/opt/wow-server", self.staging)

    def test_staging_volumes_and_networks_are_named_for_staging(self):
        for kind in ("volumes", "networks"):
            for key, name in resources(self.staging, kind).items():
                self.assertTrue(name.startswith("wow-staging"), f"{kind} {key} is named {name}")


if __name__ == "__main__":
    unittest.main()
