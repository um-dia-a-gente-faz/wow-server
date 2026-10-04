"""Tests for the character half of agent-runner (#138). The realm and the
worldserver console are fakes; the race/class table and name rules are the
real ones from scripts/create_agent_roster.py. The protocol call itself
(CMSG_CHAR_CREATE) is covered at packet level in agent/tests and needs a
live check, see the PR's How to test."""

import json
import sys
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import characters
import runner
from test_runner import PASSWORD, REPO, TOKEN, RunnerCase


class World:
    """What the fake realm holds, and what was done to it."""

    def __init__(self):
        self.accounts = {"AGENT07": []}
        self.created, self.accounts_created, self.logins = [], [], []
        self.fail_code = None
        self.down = False
        self.console_works = True


def char(name, race=10, class_=8, level=1):
    return {"guid": 1, "name": name, "race": race, "class_": class_, "gender": 0, "level": level}


def fake_car(world):
    real = characters.load_script(REPO, "create_agent_roster")

    class Session:
        def __init__(self, account):
            self.account = account

        def enum_characters(self):
            return list(world.accounts[self.account])

        def create_character(self, name, race, class_, gender=0):
            if world.fail_code:
                raise RuntimeError(f"char create failed for {name!r}: response code {world.fail_code}")
            world.created.append((self.account, name, race, class_, gender))
            world.accounts[self.account].append(char(name, race, class_))
            return 47

        def logout(self):
            pass

    class Realm:
        def __init__(self, host, port):
            pass

        def open(self, account, password):
            if world.down:
                raise ConnectionRefusedError("realm down")
            world.logins.append(password)
            return Session(account) if account in world.accounts else None

    class Console:
        def __init__(self, host):
            pass

        def create_account(self, account, password):
            world.accounts_created.append((account, password))
            if world.console_works:
                world.accounts[account] = []

    return types.SimpleNamespace(HORDE=real.HORDE, CLASS_NAMES=real.CLASS_NAMES,
                                 valid_name=real.valid_name, gen_name=real.gen_name,
                                 Realm=Realm, Console=Console)


class CharCase(RunnerCase):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.fleet._character_service = characters.CharacterService(
            fake_car(self.world), "127.0.0.1", 3724, env={"AGENT_PASSWORD": PASSWORD},
            sleep=lambda s: None)

    def post_json(self, path, body, token=TOKEN):
        r = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body).encode(),
                                   headers={"Authorization": f"Bearer {token}",
                                            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def create(self, **over):
        body = {"account": "AGENT07", "name": "Sunleaf", "race": 10, "class": 8}
        body.update(over)
        return self.post_json("/agents", body)

    def state(self):
        return json.loads((self.runtime / "state.json").read_text())


class CreateTest(CharCase):
    def test_creates_a_level_one_character_records_it_and_starts_the_container(self):
        status, body = self.create()
        self.assertEqual(status, 201)
        self.assertEqual(self.world.created, [("AGENT07", "Sunleaf", 10, 8, 0)])
        self.assertEqual(body["character"]["level"], 1)
        self.assertEqual(body["agent"]["name"], "Sunleaf")
        self.assertEqual(body["agent"]["account"], "AGENT07")
        self.assertIn("container", body["agent"])  # same shape as GET /agents/<name>
        self.assertEqual(self.state()["agents"][0]["character"], "Sunleaf")
        up = [c for c in self.compose_calls() if "up" in c]
        self.assertEqual(up[-1][-1], "agent-sunleaf")
        compose = (self.runtime / "docker-compose.agents.yml").read_text()
        self.assertIn("AGENT_NAME: Sunleaf", compose)   # audit dir is <audit>/<AGENT_NAME>
        self.assertIn("WOW_CHARACTER: Sunleaf", compose)

    def test_the_new_agent_shows_in_the_fleet_list_with_the_same_columns(self):
        self.create()
        agents = {a["name"]: a for a in self.req("/agents")[1]["agents"]}
        self.assertEqual(set(agents["Sunleaf"]), set(agents["Alpha"]))

    def test_name_is_generated_when_not_given(self):
        status, body = self.create(name=None)
        self.assertEqual(status, 201)
        self.assertTrue(body["character"]["character"].isalpha())
        self.assertEqual(self.world.created[0][1], body["character"]["character"])

    def test_taken_name_is_reported_verbatim_with_no_retry_and_no_state(self):
        self.world.fail_code = 50
        status, body = self.create()
        self.assertEqual((status, body["code"]), (409, 50))
        self.assertIn("CHAR_CREATE_NAME_IN_USE", body["error"])
        self.assertEqual(self.world.created, [])
        self.assertFalse((self.runtime / "state.json").exists())
        self.assertEqual(self.compose_calls(), [])

    def test_rejected_name_and_other_server_refusals(self):
        self.world.fail_code = 95
        status, body = self.create()
        self.assertEqual((status, body["code"]), (400, 95))
        self.world.fail_code = 46
        status, body = self.create()
        self.assertEqual((status, body["code"]), (502, 46))
        self.assertFalse((self.runtime / "state.json").exists())

    def test_bad_requests_are_refused_before_the_realm_is_touched(self):
        cases = {
            "death knight": dict(race=2, **{"class": 6}),
            "alliance race": dict(race=1),
            "class not allowed for race": dict(race=6, **{"class": 8}),
            "bad account": dict(account="AGENT26"),
            "other account": dict(account="Rubens"),
            "bad name": dict(name="Aaab1"),
            "name too long": dict(name="Abcdefghijklmn"),
            "string race": dict(race="10"),
            "bool class": dict(**{"class": True}),
            "bad gender": dict(gender=2),
        }
        for label, over in cases.items():
            status, body = self.create(**over)
            self.assertEqual(status, 400, label)
            self.assertIn("error", body, label)
        self.assertEqual(self.world.logins, [])
        self.assertEqual(self.world.created, [])

    def test_death_knight_message_says_why(self):
        self.assertIn("level-55", self.create(race=2, **{"class": 6})[1]["error"])

    def test_account_that_already_has_a_character_is_refused(self):
        self.world.accounts["AGENT07"] = [char("Existing")]
        status, body = self.create()
        self.assertEqual(status, 409)
        self.assertIn("one per account", body["error"])
        self.assertEqual(self.world.created, [])

    def test_name_already_in_the_roster_is_refused_locally(self):
        status, body = self.create(name="Alpha")
        self.assertEqual(status, 409)
        self.assertEqual(self.world.logins, [])

    def test_missing_account_is_created_through_the_console(self):
        status, _ = self.create(account="AGENT08")
        self.assertEqual(status, 201)
        self.assertEqual([a for a, _ in self.world.accounts_created], ["AGENT08"])
        self.assertEqual(self.world.created[0][0], "AGENT08")

    def test_account_that_never_appears_is_a_clear_error_with_no_state(self):
        self.world.console_works = False
        status, body = self.create(account="AGENT08")
        self.assertEqual(status, 502)
        self.assertIn("login still fails", body["error"])
        self.assertFalse((self.runtime / "state.json").exists())

    def test_realm_down_is_an_error_not_a_stack_trace(self):
        self.world.down = True
        status, body = self.create()
        self.assertEqual(status, 502)
        self.assertIn("realm unreachable", body["error"])
        self.assertFalse((self.runtime / "state.json").exists())

    def test_container_failure_reports_the_half_done_state(self):
        self.set_state(containers={}, image_created="2025-01-01T00:00:00Z",
                       compose_fail="error: boom")
        status, body = self.create()
        self.assertEqual(status, 502)
        self.assertEqual(body["partial"], {"character_created": True, "account": "AGENT07",
                                           "character": "Sunleaf", "level": 1, "recorded": True,
                                           "container_started": False})
        self.assertIn("POST /agents/Sunleaf/start", body["error"])
        self.assertEqual(self.state()["agents"][0]["character"], "Sunleaf")  # recorded: retry works
        self.set_state(containers={}, image_created="2025-01-01T00:00:00Z")
        self.assertEqual(self.req("/agents/Sunleaf/start", "POST")[0], 200)

    def test_missing_password_is_a_503_and_nothing_happens(self):
        self.fleet._character_service.env = {}
        status, body = self.create()
        self.assertEqual(status, 503)
        self.assertEqual(self.world.created, [])

    def test_body_must_be_a_json_object(self):
        for raw in (b"", b"[1]", b"not json"):
            r = urllib.request.Request(self.base + "/agents", method="POST", data=raw,
                                       headers={"Authorization": f"Bearer {TOKEN}"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(r, timeout=10)
            with cm.exception:
                self.assertEqual(cm.exception.code, 400)

    def test_requires_the_token(self):
        self.assertEqual(self.post_json("/agents", {}, token="wrong")[0], 401)
        self.assertEqual(self.world.logins, [])

    def test_password_never_leaves_the_realm_call(self):
        self.world.accounts_created.clear()
        with self.assertLogs("agent-runner", "INFO") as cm:
            ok = self.create(account="AGENT08")
            self.world.fail_code = 50
            bad = self.create(account="AGENT09", name="Moonbrook")
        out = json.dumps([ok, bad]) + "".join(r.getMessage() for r in cm.records)
        out += (self.runtime / "state.json").read_text()
        out += (self.runtime / "docker-compose.agents.yml").read_text()
        self.assertNotIn(PASSWORD, out)
        self.assertEqual(set(self.world.logins), {PASSWORD})  # but it did reach the realm

    def test_create_is_logged_with_caller_and_result(self):
        with self.assertLogs("agent-runner", "INFO") as cm:
            self.create()
        line = json.loads(cm.records[-1].getMessage())
        self.assertEqual((line["action"], line["agent"], line["caller"], line["result"]),
                         ("create", "Sunleaf", "127.0.0.1", "ok"))


class ListTest(CharCase):
    def test_lists_what_the_realm_has(self):
        self.world.accounts["AGENT07"] = [char("Sunleaf")]
        status, body = self.req("/characters?account=AGENT07")
        self.assertEqual(status, 200)
        self.assertEqual(body["characters"], [{"name": "Sunleaf", "race": 10, "class": 8,
                                               "gender": 0, "level": 1}])
        self.assertFalse(body["free_slot"])

    def test_free_slot_and_missing_account_and_no_side_effects(self):
        self.assertTrue(self.req("/characters?account=AGENT07")[1]["free_slot"])
        status, body = self.req("/characters?account=AGENT09")
        self.assertEqual((status, body["exists"], body["free_slot"]), (200, False, True))
        self.assertEqual(self.world.accounts_created, [])

    def test_bad_account_and_missing_token(self):
        self.assertEqual(self.req("/characters?account=Rubens")[0], 400)
        self.assertEqual(self.req("/characters")[0], 400)
        self.assertEqual(self.req("/characters?account=AGENT07", token=None)[0], 401)

    def test_realm_down(self):
        self.world.down = True
        self.assertEqual(self.req("/characters?account=AGENT07")[0], 502)


class RetireTest(CharCase):
    def test_retire_stops_marks_and_keeps_the_character(self):
        status, body = self.req("/agents/Alpha/retire", "POST")
        self.assertEqual(status, 200)
        self.assertTrue(body["agent"]["retired"])
        self.assertEqual(self.compose_calls()[-1][-2:], ["stop", "agent-alpha"])
        self.assertTrue(self.state()["agents"][0]["retired"])
        self.assertEqual(self.world.created, [])  # nothing on the realm was touched
        self.assertEqual(self.world.logins, [])
        agents = {a["name"]: a for a in self.req("/agents")[1]["agents"]}
        self.assertTrue(agents["Alpha"]["retired"])
        self.assertFalse(agents["Bravo"]["retired"])

    def test_retire_is_idempotent_and_fine_when_the_container_is_gone(self):
        self.assertEqual(self.req("/agents/Bravo/retire", "POST")[0], 200)  # Bravo: absent
        self.assertEqual(self.req("/agents/Bravo/retire", "POST")[0], 200)
        self.assertEqual(len(self.state()["agents"]), 1)

    def test_retired_agent_cannot_be_started(self):
        self.req("/agents/Bravo/retire", "POST")
        before = len(self.compose_calls())
        status, body = self.req("/agents/Bravo/start", "POST")
        self.assertEqual(status, 409)
        self.assertIn("retired", body["error"])
        self.assertEqual(len(self.compose_calls()), before)

    def test_ports_of_the_other_agents_do_not_move_when_one_retires(self):
        before = {e["name"]: e["port"] for e in self.fleet.roster()}
        self.req("/agents/Alpha/retire", "POST")
        self.assertEqual({e["name"]: e["port"] for e in self.fleet.roster()}, before)

    def test_unknown_agent_and_token(self):
        self.assertEqual(self.req("/agents/Nobody/retire", "POST")[0], 404)
        self.assertEqual(self.req("/agents/Alpha/retire", "POST", token=None)[0], 401)


if __name__ == "__main__":
    unittest.main()
