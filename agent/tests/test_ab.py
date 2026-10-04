"""Offline A/B helper tests; these never contact a provider."""
import json
import os
import tempfile
import unittest

from agent.tools.ab import FixtureJev, _action_key, run


class FixtureJevTest(unittest.TestCase):
    def test_fixture_matches_snapshot_and_candidates(self):
        snapshot = {"me": {"health": 10}}
        options = [{"action": "idle", "params": {}}]
        record = {"input_hash": FixtureJev.input_hash(snapshot, options), "action": "idle",
                  "params": {}, "confidence": .9, "usage": {"input_tokens": 5}}
        with tempfile.NamedTemporaryFile("w", encoding="utf8", delete=False) as f:
            f.write(json.dumps(record) + "\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        client = FixtureJev(path)
        self.assertEqual(client.choose_action(snapshot, options), ("idle", {}))
        self.assertEqual(client.last_confidence, .9)
        self.assertEqual(client.last_usage["input_tokens"], 5)

    def test_fixture_refuses_unmatched_inputs(self):
        client = FixtureJev.__new__(FixtureJev)
        client.entries = {}
        with self.assertRaises(ValueError):
            client.choose_action({}, [])


class ActionKeyTest(unittest.TestCase):
    def test_key_ignores_mapping_order(self):
        self.assertEqual(_action_key("a", {"x": 1, "y": 2}),
                         _action_key("a", {"y": 2, "x": 1}))

    def test_call_budget_refuses_before_provider_configuration(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf8", delete=False) as f:
            f.write(json.dumps({"cycle": 1, "snapshot": {}, "tool_call": {}}) + "\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        with self.assertRaisesRegex(ValueError, "exceeds --max-calls"):
            run([path], ["llm"], max_calls=0)


if __name__ == "__main__":
    unittest.main()
