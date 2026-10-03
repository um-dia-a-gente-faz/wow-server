"""UM-100: drive the real agent.jev.JevClient against a jev-mock running as its
own process (CI starts one; so does `docker compose --profile jev-mock`).
Unlike test_server.py, nothing here is in-process with the mock. Skipped unless
JEV_MOCK_URL names it, e.g. JEV_MOCK_URL=http://127.0.0.1:8090/api/alpha."""

import os
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from agent import jev  # noqa: E402

URL = os.environ.get("JEV_MOCK_URL")
CANDIDATES = [{"action": "attack", "params": {"guid": 0x1234}},
              {"action": "loot", "params": {"guid": 0x5678}},
              {"action": "rest", "params": {}}]


@unittest.skipUnless(URL, "JEV_MOCK_URL not set; start tools/jev-mock/server.py and point it there")
class ExternalMockTest(unittest.TestCase):
    def test_jev_client_round_trip(self):
        client = jev.JevClient(URL, "typesafe/jev-1.13")
        offered = [(c["action"], c["params"]) for c in CANDIDATES]
        for _ in range(5):
            self.assertIn(client.choose_action({"me": {"hp": 50}}, CANDIDATES), offered)
            self.assertIn(client.last_choice, client.last_probabilities)
            self.assertIsNotNone(client.last_confidence)


if __name__ == "__main__":
    unittest.main()
