"""agents/roster.py: the typed roster loader and its validation (#557, pure part of #391).

Runs from the repo root (`discover -t . -s agent/tests`), so `agents` imports as a
namespace package. No network; the compose file is read as text and never parsed."""
import json
import re
import unittest
from pathlib import Path

from agents import roster
from agents.roster import RosterError, parse_roster

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.agents.yml"
SERVICE_HEADER = re.compile(r"  ([a-z0-9-]+):$")


def entry(account="AGENT01", character="Luaprata", race=None, class_=None, gender=None, mission=None):
    out = {"account": account, "character": character, "race": race, "class": class_, "gender": gender}
    if mission is not None:
        out["mission"] = mission
    return out


def data(*entries):
    return {"agents": list(entries)}


def service_blocks(text):
    """{service name: its lines}. A block starts at a `  <name>:` header line and ends at the
    next header or top-level key; only the service lines matter to the tests."""
    blocks, name = {}, None
    for line in text.splitlines():
        header = SERVICE_HEADER.fullmatch(line)
        if header:
            name = header.group(1)
            blocks[name] = []
        elif line and not line.startswith(" "):
            name = None
        elif name is not None:
            blocks[name].append(line.strip())
    return blocks


class CommittedRosterTests(unittest.TestCase):
    def setUp(self):
        self.agents = roster.load_roster()

    def test_committed_roster_has_25_agents(self):
        self.assertEqual(len(self.agents), 25)

    def test_first_agent_derivations(self):
        first = self.agents[0]
        self.assertEqual(first.index, 1)
        self.assertEqual(first.account, "AGENT01")
        self.assertEqual(first.character, "Luaprata")
        self.assertEqual(first.api_port, 9601)
        self.assertEqual(first.service, "agent-luaprata")
        self.assertEqual(first.container, "wow-agent-luaprata")
        self.assertEqual(first.profiles, ("party", "raid"))

    def test_sixth_agent_is_raid_only(self):
        self.assertEqual(self.agents[5].index, 6)
        self.assertEqual(self.agents[5].profiles, ("raid",))

    def test_last_agent_port(self):
        self.assertEqual(self.agents[-1].index, 25)
        self.assertEqual(self.agents[-1].api_port, 9625)

    def test_to_json_gives_back_the_file_entries(self):
        file_agents = json.loads(roster.ROSTER_PATH.read_text())["agents"]
        self.assertEqual([a.to_json() for a in self.agents], file_agents)

    def test_to_json_keeps_key_order_and_class_key(self):
        keys = list(self.agents[0].to_json())
        self.assertEqual(keys, ["account", "character", "race", "class", "gender"])
        self.assertEqual(self.agents[5].to_json()["class"], 5)

    def test_missing_file_is_not_handled(self):
        with self.assertRaises(FileNotFoundError):
            roster.load_roster(REPO / "agents" / "no-such-roster.json")


class ComposeAgreementTests(unittest.TestCase):
    """The derivations match what docker-compose.agents.yml says today, read from the file."""

    @classmethod
    def setUpClass(cls):
        cls.blocks = service_blocks(COMPOSE.read_text())
        cls.agents = roster.load_roster()

    def test_one_agent_service_per_roster_entry(self):
        services = sorted(n for n in self.blocks if n.startswith("agent-"))
        self.assertEqual(services, sorted(a.service for a in self.agents))

    def test_each_service_has_container_and_port(self):
        for a in self.agents:
            with self.subTest(agent=a.account):
                self.assertIn(a.service, self.blocks, "service block missing")
                lines = self.blocks[a.service]
                self.assertIn(f"container_name: {a.container}", lines)
                self.assertIn(f"AGENT_HTTP_PORT: {a.api_port}", lines)
                self.assertIn(f"profiles: [{', '.join(a.profiles)}]", lines)


class ValidationTests(unittest.TestCase):
    def assertRejects(self, raw, index, field):
        with self.assertRaises(RosterError) as cm:
            parse_roster(raw)
        msg = str(cm.exception)
        self.assertIn(f"entry {index}", msg)
        self.assertIn(f"'{field}'", msg)

    def test_rule1_top_level_must_be_object_with_agents_list(self):
        for raw in ([], "agents", None, {}, {"agents": {}}, {"agents": "x"}, {"other": []}):
            with self.subTest(raw=raw), self.assertRaises(RosterError) as cm:
                parse_roster(raw)
            self.assertIn("agents", str(cm.exception))

    def test_rule2_missing_key_is_rejected(self):
        broken = entry("AGENT02", "Farstrider")
        del broken["race"]
        with self.assertRaises(RosterError) as cm:
            parse_roster(data(entry(), broken))
        msg = str(cm.exception)
        self.assertIn("entry 2", msg)
        self.assertIn("missing ['race']", msg)

    def test_rule2_unknown_key_is_rejected(self):
        typo = entry("AGENT02", "Farstrider")
        typo["charcter"] = typo.pop("character")
        with self.assertRaises(RosterError) as cm:
            parse_roster(data(entry(), typo))
        msg = str(cm.exception)
        self.assertIn("entry 2", msg)
        self.assertIn("charcter", msg)

    def test_rule2_extra_key_is_rejected(self):
        extra = entry("AGENT02", "Farstrider")
        extra["retired"] = True
        with self.assertRaises(RosterError) as cm:
            parse_roster(data(entry(), extra))
        msg = str(cm.exception)
        self.assertIn("entry 2", msg)
        self.assertIn("retired", msg)

    def test_rule2_entry_must_be_object(self):
        with self.assertRaises(RosterError) as cm:
            parse_roster(data(entry(), ["AGENT02"]))
        self.assertIn("entry 2", str(cm.exception))

    def test_rule3_account_format(self):
        for bad in ("AGENT1", "AGENT001", "AGENT00", "agent01", "AGENT0A", "XAGENT01", "AGENT01\n", 1, None):
            with self.subTest(account=bad):
                self.assertRejects(data(entry(bad)), 1, "account")

    def test_rule3_account_is_unique(self):
        self.assertRejects(data(entry(), entry("AGENT01", "Farstrider")), 2, "account")

    def test_rule4_character_must_be_alphanumeric_text(self):
        for bad in ("", "Lua prata", "Lua-prata", None, 7):
            with self.subTest(character=bad):
                self.assertRejects(data(entry(character=bad)), 1, "character")

    def test_rule4_character_must_be_ascii(self):
        for bad in ("Ébano", "Lua²"):
            with self.subTest(character=bad):
                self.assertRejects(data(entry(character=bad)), 1, "character")

    def test_rule4_character_is_unique_case_insensitively(self):
        self.assertRejects(data(entry(), entry("AGENT02", "luaprata")), 2, "character")

    def test_rule5_race_class_gender_are_int_or_none(self):
        for field in ("race", "class", "gender"):
            for bad in ("8", 8.0, [], True, False):
                with self.subTest(field=field, value=bad):
                    raw = entry()
                    raw[field] = bad
                    self.assertRejects(data(raw), 1, field)

    def test_rule5_zero_and_none_are_valid(self):
        agents = parse_roster(data(entry(race=0, class_=0, gender=0)))
        self.assertEqual((agents[0].race, agents[0].class_id, agents[0].gender), (0, 0, 0))

    def test_rule6_empty_roster_is_valid(self):
        self.assertEqual(parse_roster({"agents": []}), [])

    def test_mission_is_optional_and_defaults_to_level_to_cap(self):
        self.assertEqual(parse_roster(data(entry()))[0].mission, "level_to_cap")

    def test_default_mission_is_left_out_of_to_json(self):
        self.assertNotIn("mission", parse_roster(data(entry()))[0].to_json())

    def test_non_default_mission_round_trips_through_to_json(self):
        raw = parse_roster(data(entry(mission="explore")))[0].to_json()
        self.assertEqual(raw, {**entry(), "mission": "explore"})
        self.assertEqual(parse_roster(data(raw))[0].to_json(), raw)

    def test_mission_accepts_every_mission_in_the_list(self):
        for m in roster.MISSIONS:
            with self.subTest(mission=m):
                self.assertEqual(parse_roster(data(entry(mission=m)))[0].mission, m)

    def test_unknown_mission_is_rejected(self):
        for bad in ("fishing", "Explore", "", 7):
            with self.subTest(mission=bad):
                self.assertRejects(data(entry(mission=bad)), 1, "mission")


if __name__ == "__main__":
    unittest.main()
