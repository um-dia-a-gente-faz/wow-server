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

    def test_error_bodies_carry_version(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/nope", timeout=5)
        body = json.loads(cm.exception.read())
        cm.exception.close()
        self.assertEqual(ac.validate(body, C["errors"]["schema"]), [])


class DocTest(unittest.TestCase):
    def test_endpoint_reference_is_current(self):
        self.assertEqual((ROOT / "docs" / "AGENT-API.md").read_text("utf-8"), ac.render_markdown(),
                         "run: python3 -m agent.api_contract > docs/AGENT-API.md")


if __name__ == "__main__":
    unittest.main()
