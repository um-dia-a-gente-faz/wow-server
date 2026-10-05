"""UM-63: agent roster planning, idempotent creation, and the generated compose file.

No test here touches the network: Realm and Console are replaced by fakes, and the
dry-run tests make any real socket or HTTP call fail loudly."""
import contextlib
import io
import json
import os
import random
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import create_agent_roster as car  # noqa: E402
import gen_agents_compose as gen   # noqa: E402

DEATH_KNIGHT = 6
HORDE_RACES = {2, 5, 6, 8, 10}     # orc, undead, tauren, troll, blood elf
# class ids each Horde race may create (ChrRaces/ChrClasses combinations in 3.3.5a),
# written out independently of car.HORDE on purpose
VALID_CLASSES = {
    2: {1, 3, 4, 7, 9},
    5: {1, 4, 5, 8, 9},
    6: {1, 3, 7, 11},
    8: {1, 3, 4, 5, 7, 8},
    10: {1, 2, 3, 4, 5, 8, 9},
}


class FakeSession:
    def __init__(self, realm, account):
        self.realm, self.account = realm, account
        self.closed = False

    def enum_characters(self):
        return list(self.realm.chars.get(self.account, []))

    def create_character(self, name, race, class_, gender=0, **_):
        self.realm.create_calls.append(name)
        if name.lower() in self.realm.taken_names:
            raise RuntimeError(f"char create failed for {name!r}: response code {self.realm.name_error}")
        if self.realm.fail_code:
            raise RuntimeError(f"char create failed for {name!r}: response code {self.realm.fail_code}")
        self.realm.taken_names.add(name.lower())
        self.realm.chars[self.account] = [{"guid": len(self.realm.chars) + 1, "name": name, "race": race,
                                           "class_": class_, "gender": gender, "level": 1}]

    def logout(self):
        self.closed = True


class FakeRealm:
    """Stands in for create_agent_roster.Realm; `accounts` is the set the realm knows."""

    def __init__(self, accounts=(), chars=None, taken_names=(), name_error=50, fail_code=0):
        self.accounts = set(accounts)
        self.chars = dict(chars or {})
        self.taken_names = {n.lower() for n in taken_names}
        self.name_error, self.fail_code = name_error, fail_code
        self.create_calls = []
        self.sessions = []

    def open(self, account, password):
        if account not in self.accounts:
            return None
        s = FakeSession(self, account)
        self.sessions.append(s)
        return s


class FakeConsole:
    def __init__(self, realm, works=True):
        self.realm, self.works, self.created = realm, works, []

    def create_account(self, account, password):
        self.created.append(account)
        if self.works:
            self.realm.accounts.add(account)


class PlanTests(unittest.TestCase):
    def test_deterministic_for_a_seed(self):
        a = car.plan_agents(random.Random(7), [])
        b = car.plan_agents(random.Random(7), [])
        c = car.plan_agents(random.Random(8), [])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_shape_and_legacy_entries(self):
        plan = car.plan_agents(random.Random(1), [])
        self.assertEqual([a["account"] for a in plan], [f"AGENT{n:02d}" for n in range(1, 26)])
        self.assertEqual([a["character"] for a in plan[:5]],
                         ["Luaprata", "Farstrider", "Shadowblade", "Sunspeaker", "Spellweaver"])
        for a in plan[:5]:
            self.assertEqual((a["race"], a["class"], a["gender"]), (None, None, None))

    def test_planned_races_are_horde_classes_valid_never_death_knight(self):
        for seed in range(200):
            for a in car.plan_agents(random.Random(seed), [])[5:]:
                self.assertIn(a["race"], HORDE_RACES)
                self.assertIn(a["class"], VALID_CLASSES[a["race"]])
                self.assertNotEqual(a["class"], DEATH_KNIGHT)
                self.assertIn(a["gender"], (0, 1))

    def test_every_horde_race_and_class_table_entry_is_valid(self):
        self.assertEqual({r: set(v[1]) for r, v in car.HORDE.items()}, VALID_CLASSES)

    def test_names_valid_and_unique(self):
        for seed in range(200):
            names = [a["character"] for a in car.plan_agents(random.Random(seed), [])]
            self.assertTrue(all(car.valid_name(n) for n in names), names)
            self.assertEqual(len({n.lower() for n in names}), len(names), names)

    def test_valid_name_rules(self):
        for ok in ("Zanja", "Ab", "Velkaelanar"):
            self.assertTrue(car.valid_name(ok), ok)
        for bad in ("A", "Abcdefghijklm", "Zan1a", "Zan ja", "Aaab", "Zanjá"):
            self.assertFalse(car.valid_name(bad), bad)

    def test_existing_entries_are_kept(self):
        kept = {"account": "AGENT07", "character": "Mine", "race": 8, "class": 5, "gender": 1}
        plan = car.plan_agents(random.Random(3), [kept])
        self.assertIs(plan[6], kept)
        again = car.plan_agents(random.Random(99), plan)
        self.assertEqual(again, plan)          # a full roster plans nothing new, whatever the seed

    def test_new_names_avoid_names_already_on_the_roster(self):
        taken = {"account": "AGENT06", "character": "Zanja", "race": 8, "class": 5, "gender": 0}
        for seed in range(100):
            names = [a["character"].lower() for a in car.plan_agents(random.Random(seed), [taken])]
            self.assertEqual(len(set(names)), len(names))


class EnsureAgentTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(5)
        self.entry = {"account": "AGENT06", "character": "Zanja", "race": 8, "class": 5, "gender": 1}

    def run_ensure(self, realm, console, entry=None, taken=None):
        entry = self.entry if entry is None else entry
        taken = {entry["character"].lower()} if taken is None else taken
        return car.ensure_agent(entry, "pw", realm, console, self.rng, taken, log=lambda *_: None,
                                sleep=lambda *_: None)

    def test_creates_account_and_character_then_second_run_changes_nothing(self):
        realm = FakeRealm()
        console = FakeConsole(realm)
        self.assertEqual(self.run_ensure(realm, console), "created")
        self.assertEqual(console.created, ["AGENT06"])
        self.assertEqual(realm.chars["AGENT06"][0]["name"], "Zanja")
        self.assertEqual(realm.create_calls, ["Zanja"])

        self.assertEqual(self.run_ensure(realm, console), "exists")
        self.assertEqual(console.created, ["AGENT06"])      # no second account create
        self.assertEqual(realm.create_calls, ["Zanja"])      # no second char create

    def test_existing_character_is_read_back_into_the_entry(self):
        realm = FakeRealm(accounts={"AGENT01"}, chars={"AGENT01": [
            {"guid": 1, "name": "Luaprata", "race": 10, "class_": 8, "gender": 1, "level": 5}]})
        entry = {"account": "AGENT01", "character": "Luaprata", "race": None, "class": None, "gender": None}
        self.assertEqual(self.run_ensure(realm, FakeConsole(realm), entry), "exists")
        self.assertEqual((entry["race"], entry["class"], entry["gender"]), (10, 8, 1))
        self.assertEqual(realm.create_calls, [])

    def test_existing_account_without_character_creates_only_the_character(self):
        realm = FakeRealm(accounts={"AGENT06"})
        console = FakeConsole(realm)
        self.assertEqual(self.run_ensure(realm, console), "created")
        self.assertEqual(console.created, [])

    def test_name_in_use_retries_with_a_new_name(self):
        realm = FakeRealm(accounts={"AGENT06"}, taken_names={"Zanja"})
        taken = {"zanja"}
        self.assertEqual(self.run_ensure(realm, FakeConsole(realm), taken=taken), "created")
        self.assertEqual(len(realm.create_calls), 2)
        self.assertNotEqual(self.entry["character"], "Zanja")
        self.assertTrue(car.valid_name(self.entry["character"]))
        self.assertEqual(realm.chars["AGENT06"][0]["name"], self.entry["character"])
        self.assertIn(self.entry["character"].lower(), taken)

    def test_name_rejection_codes_all_retry(self):
        for code in (50, 88, 95, 102):
            realm = FakeRealm(accounts={"AGENT06"}, taken_names={"Zanja"}, name_error=code)
            entry = dict(self.entry)
            self.assertEqual(self.run_ensure(realm, FakeConsole(realm), entry), "created", code)

    def test_other_errors_raise_without_retrying(self):
        for code in (48, 49, 51, 56, 103):     # e.g. CHAR_CREATE_ERROR, FAILED, SERVER_LIMIT, ...
            realm = FakeRealm(accounts={"AGENT06"}, fail_code=code)
            with self.assertRaises(RuntimeError, msg=code):
                self.run_ensure(realm, FakeConsole(realm), dict(self.entry))
            self.assertEqual(len(realm.create_calls), 1, code)

    def test_gives_up_after_ten_name_clashes(self):
        class Always(FakeRealm):
            def open(self, account, password):
                s = super().open(account, password)
                if s:
                    s.create_character = lambda *a, **k: (_ for _ in ()).throw(
                        RuntimeError("char create failed for 'x': response code 50"))
                return s
        realm = Always(accounts={"AGENT06"})
        with self.assertRaisesRegex(RuntimeError, "no free name"):
            self.run_ensure(realm, FakeConsole(realm))

    def test_login_still_failing_after_account_create_raises(self):
        realm = FakeRealm()
        with self.assertRaisesRegex(RuntimeError, "login still fails"):
            self.run_ensure(realm, FakeConsole(realm, works=False))

    def test_account_without_character_and_no_plan_raises(self):
        realm = FakeRealm(accounts={"AGENT01"})
        entry = {"account": "AGENT01", "character": "Luaprata", "race": None, "class": None, "gender": None}
        with self.assertRaisesRegex(RuntimeError, "none planned"):
            self.run_ensure(realm, FakeConsole(realm), entry)

    def test_session_is_always_logged_out(self):
        realm = FakeRealm(accounts={"AGENT06"}, fail_code=48)
        with self.assertRaises(RuntimeError):
            self.run_ensure(realm, FakeConsole(realm))
        self.assertTrue(all(s.closed for s in realm.sessions))


@contextlib.contextmanager
def no_network():
    def boom(*a, **k):
        raise AssertionError("network access attempted")
    with mock.patch.object(socket, "create_connection", boom), \
            mock.patch.object(socket.socket, "connect", boom), \
            mock.patch.object(urllib.request, "urlopen", boom):
        yield


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "agents" / "roster.json"
        p = mock.patch.object(car, "ROSTER_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def main(self, *argv, env=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out):
            rc = car.main(list(argv))
        return rc, out.getvalue()

    def test_dry_run_makes_no_network_call_and_writes_nothing(self):
        with no_network():
            rc, out = self.main("--dry-run", "--seed", "1")
        self.assertEqual(rc, 0)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(out.count("\n  + "), 20)     # 20 planned characters
        self.assertIn("AGENT25", out)

    def test_dry_run_does_not_construct_realm_or_console(self):
        with mock.patch.object(car, "Realm", side_effect=AssertionError), \
                mock.patch.object(car, "Console", side_effect=AssertionError):
            self.assertEqual(self.main("--dry-run")[0], 0)

    def test_write_plan_is_deterministic_offline_and_roundtrips(self):
        with no_network():
            self.assertEqual(self.main("--write-plan")[0], 0)
        first = self.path.read_text()
        self.path.unlink()
        with no_network():
            self.main("--write-plan")
        self.assertEqual(self.path.read_text(), first)
        agents = json.loads(first)["agents"]
        self.assertEqual(len(agents), 25)
        # planning again with another seed must not change a committed roster
        with no_network():
            self.main("--write-plan", "--seed", "12345")
        self.assertEqual(self.path.read_text(), first)

    def test_real_run_needs_a_password_and_touches_nothing_without_one(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True), no_network(), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = car.main([])
        self.assertEqual(rc, 2)
        self.assertFalse(self.path.exists())

    def test_real_run_twice_is_idempotent(self):
        realm = FakeRealm(accounts={f"AGENT0{n}" for n in range(1, 6)}, chars={
            f"AGENT0{n}": [{"guid": n, "name": name, "race": 2, "class_": 1, "gender": 0, "level": 10}]
            for n, name in enumerate(["Luaprata", "Farstrider", "Shadowblade", "Sunspeaker", "Spellweaver"], 1)})
        console = FakeConsole(realm)
        with mock.patch.object(car, "Realm", lambda *a: realm), mock.patch.object(car, "Console", lambda *a: console):
            rc, out = self.main("--seed", "4", env={"AGENT_PASSWORD": "pw"})
            self.assertEqual(rc, 0, out)
            self.assertIn("20 created, 5 already there", out)
            roster = self.path.read_text()
            self.assertEqual(len(console.created), 20)
            creates = list(realm.create_calls)

            rc, out = self.main("--seed", "999", env={"AGENT_PASSWORD": "pw"})
            self.assertEqual(rc, 0)
            self.assertIn("0 created, 25 already there", out)
            self.assertEqual(len(console.created), 20)
            self.assertEqual(realm.create_calls, creates)
            self.assertEqual(self.path.read_text(), roster)
        agents = json.loads(roster)["agents"]
        self.assertTrue(all(a["race"] in HORDE_RACES for a in agents))
        self.assertEqual({a["character"] for a in agents}, {c[0]["name"] for c in realm.chars.values()})
        self.assertNotIn("pw", roster)


class CommittedFilesTests(unittest.TestCase):
    def test_committed_roster_is_the_seeded_plan(self):
        committed = car.load_roster(REPO / "agents" / "roster.json")
        self.assertEqual(committed, car.plan_agents(random.Random(car.PLAN_SEED), []))
        self.assertEqual(len(committed), 25)
        for a in committed[5:]:
            self.assertIn(a["race"], HORDE_RACES)
            self.assertIn(a["class"], VALID_CLASSES[a["race"]])

    def test_committed_roster_has_no_secrets(self):
        text = (REPO / "agents" / "roster.json").read_text().lower()
        for word in ("password", "secret", "token", "key"):
            self.assertNotIn(word, text)

    def test_committed_compose_matches_generator(self):
        want = gen.render(gen.load_roster())
        self.assertEqual((REPO / "docker-compose.agents.yml").read_text(), want,
                         "docker-compose.agents.yml is stale; run scripts/gen_agents_compose.py")

    def test_generated_services_profiles_ports_and_legacy_names(self):
        text = gen.render(gen.load_roster())
        self.assertIn("AGENT_THINK_INTERVAL_S: ${AGENT_THINK_INTERVAL_S:-5}\n", text)
        for n in ("luaprata", "farstrider", "shadowblade", "sunspeaker", "spellweaver"):
            self.assertIn(f"\n  agent-{n}:\n", text)
            self.assertIn(f"container_name: wow-agent-{n}\n", text)
        self.assertEqual(text.count("profiles: [party, raid]"), 5)
        self.assertEqual(text.count("profiles: [raid]"), 20)
        ports = [int(line.split(":")[-1]) for line in text.splitlines() if "AGENT_HTTP_PORT:" in line
                 and not line.lstrip().startswith("#")]
        self.assertEqual(ports, list(range(9601, 9626)))
        self.assertEqual(text.count("mem_limit: 128m"), 1)   # in the shared x-agent defaults
        self.assertNotIn("dummy", text)

    def test_generator_rejects_duplicate_names(self):
        a = {"account": "AGENT01", "character": "Same"}
        with self.assertRaises(ValueError):
            gen.render([a, dict(a, account="AGENT02")])

    def test_port_range_is_not_used_elsewhere_in_the_compose_files(self):
        for f in ("docker-compose.yml", "monitoring/docker-compose.yml"):
            text = (REPO / f).read_text()
            for port in range(9601, 9626):
                self.assertNotIn(f"{port}:", text, f)


if __name__ == "__main__":
    unittest.main()
