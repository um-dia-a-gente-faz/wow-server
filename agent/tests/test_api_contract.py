"""#264: real agent/http_api.py responses validate against agent/api_schema.json, the
validator itself works, and docs/AGENT-API.md is the current rendering of the schema."""
import json
import pathlib
import unittest
import urllib.error
import urllib.request

from agent import api_contract as ac
from agent import http_api
from agent.tests.test_http_api import _ServerCase

C = ac.load()
ROOT = pathlib.Path(__file__).resolve().parents[2]


class ValidatorTest(unittest.TestCase):
    def test_catches_type_missing_and_nested(self):
        s = {"type": "object", "required": ["a"], "properties": {
            "a": {"type": "integer"}, "b": {"type": "array", "items": {"type": ["string", "null"]}}}}
        self.assertEqual(ac.validate({"a": 1, "b": ["x", None], "extra": 1}, s), [])
        self.assertEqual(len(ac.validate({}, s)), 1)
        self.assertEqual(len(ac.validate({"a": True}, s)), 1)  # bool is not an integer
        self.assertEqual(len(ac.validate({"a": 1, "b": [3]}, s)), 1)

    def test_sample_is_valid_for_every_response(self):
        for ep, spec in C["endpoints"].items():
            for name, schema in spec["responses"].items():
                self.assertEqual(ac.validate(ac.sample(schema), schema), [], f"{ep} {name}")


class ProducerTest(_ServerCase):
    def check(self, path, endpoint, response):
        body = json.loads(self.get(path)[1])
        self.assertEqual(ac.validate(body, ac.response_schema(endpoint, response)), [], path)
        self.assertEqual(body["api_version"], C["api_version"])

    def test_version_constant_matches_schema(self):
        self.assertEqual(http_api.API_VERSION, C["api_version"])

    def test_in_game_responses(self):
        self.observer.record_decision({"cycle": 1, "goal": "g", "model": "m", "brain": "jev", "confidence": 0.5,
                                       "fallback": "jev call failed", "tool_call": {"name": "idle", "args": {}}})
        self.check("/healthz", "/healthz", "ok")
        self.check("/state", "/state", "in game")
        self.check("/perception", "/perception", "in game")
        self.check("/brain", "/brain", "ok")
        self.check("/", "/", "ok")

    def test_detached_responses(self):
        self.observer.detach()
        self.check("/state", "/state", "not in game")
        self.check("/perception", "/perception", "not in game")
        self.check("/brain", "/brain", "ok")

    def test_state_with_a_lifecycle_validates_in_game_and_detached(self):
        from agent.lifecycle import Lifecycle, State
        lc = Lifecycle(clock=lambda: 1700000000.5)
        lc.to(State.ONLINE, "connected")
        self.observer.lifecycle = lc
        self.check("/state", "/state", "in game")
        self.assertEqual(json.loads(self.get("/state")[1])["lifecycle"], lc.snapshot())
        self.observer.detach()
        self.check("/state", "/state", "not in game")
        body = json.loads(self.get("/state")[1])
        self.assertEqual(body["lifecycle"], lc.snapshot())
        self.assertIs(body["connected"], False)

    def test_state_has_no_lifecycle_key_until_one_is_attached(self):
        self.assertNotIn("lifecycle", json.loads(self.get("/state")[1]))
        self.observer.detach()
        self.assertNotIn("lifecycle", json.loads(self.get("/state")[1]))

    def test_lifecycle_schema_matches_the_machine(self):
        from agent.lifecycle import Lifecycle, State
        for response in ("in game", "not in game"):
            with self.subTest(response=response):
                schema = ac.response_schema("/state", response)["properties"]["lifecycle"]
                self.assertEqual(schema["properties"]["state"]["enum"], [s.value for s in State])
                self.assertEqual(sorted(schema["required"]), sorted(Lifecycle().snapshot()))
                self.assertNotIn("lifecycle", ac.response_schema("/state", response)["required"])

    def test_error_bodies_carry_version(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/nope", timeout=5)
        body = json.loads(cm.exception.read())
        cm.exception.close()
        self.assertEqual(ac.validate(body, C["errors"]["schema"]), [])


class MissionContractTest(_ServerCase):
    """#314: the mission is in /healthz and both /state responses, and its enum is agent.missions.MISSIONS."""

    def test_schema_enum_is_the_missions_tuple(self):
        from agent.missions import MISSIONS
        for endpoint, response in (("/healthz", "ok"), ("/state", "in game"), ("/state", "not in game")):
            with self.subTest(endpoint=endpoint, response=response):
                schema = ac.response_schema(endpoint, response)
                self.assertIn("mission", schema["required"])
                self.assertEqual(schema["properties"]["mission"], {"type": "string", "enum": list(MISSIONS)})

    def test_responses_carry_the_observers_mission(self):
        self.observer.mission = "explore"
        self.observer.record_decision({"cycle": 1, "brain": "jev"})
        self.assertEqual(json.loads(self.get("/healthz")[1])["mission"], "explore")
        self.check("/healthz", "/healthz", "ok")
        self.check("/state", "/state", "in game")
        self.assertEqual(json.loads(self.get("/state")[1])["mission"], "explore")
        self.observer.detach()
        self.check("/state", "/state", "not in game")
        self.assertEqual(json.loads(self.get("/state")[1])["mission"], "explore")

    def check(self, path, endpoint, response):
        body = json.loads(self.get(path)[1])
        self.assertEqual(ac.validate(body, ac.response_schema(endpoint, response)), [], path)


class DocTest(unittest.TestCase):
    def test_endpoint_reference_is_current(self):
        self.assertEqual((ROOT / "docs" / "AGENT-API.md").read_text("utf-8"), ac.render_markdown(),
                         "run: python3 -m agent.api_contract > docs/AGENT-API.md")


if __name__ == "__main__":
    unittest.main()
