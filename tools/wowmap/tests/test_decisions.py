"""#326: decisions.recent and the /api/v2/characters/{name}/decisions and /{name} routes."""
import json
import os
import tempfile
import unittest
from unittest import mock

import _calibration_env  # noqa: F401
import pagesrc  # noqa: F401
import api  # noqa: E402
import character  # noqa: E402
import decisions  # noqa: E402
import state  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def rec(ts, name="loot", ok=True, conf=0.9):
    return {"ts": ts, "tool_call": {"name": name, "args": {"guid": 7}},
            "result": {"ok": ok, "error": None if ok else "boom"}, "confidence": conf, "brain": "jev"}


class RecentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        os.makedirs(os.path.join(self.dir, "Luaprata", "lifecycle"))
        os.makedirs(os.path.join(self.dir, "Luaprata", "unmet"))

    def write(self, rel, lines, raw=False):
        with open(os.path.join(self.dir, "Luaprata", rel), "w") as f:
            f.write("".join(l if raw else json.dumps(l) + "\n" for l in lines))

    def test_newest_first_across_day_files_and_limit(self):
        self.write("2026-10-17.jsonl", [rec(1), rec(2)])
        self.write("2026-10-18.jsonl", [rec(3), rec(4)])
        got = decisions.recent(self.dir, "Luaprata", 3)
        self.assertEqual([d["t"] for d in got], [4, 3, 2])
        self.assertEqual(got[0], {"t": 4, "action": "loot", "args": {"guid": 7}, "ok": True, "error": None,
                                  "confidence": 0.9, "brain": "jev", "reason": None})

    def test_only_two_newest_day_files(self):
        for i, day in enumerate(("15", "16", "17")):
            self.write(f"2026-10-{day}.jsonl", [rec(i)])
        self.assertEqual([d["t"] for d in decisions.recent(self.dir, "Luaprata", 9)], [2, 1])

    def test_lifecycle_and_unmet_ignored(self):
        self.write("2026-10-18.jsonl", [rec(1)])
        self.write("lifecycle/2026-10-18.jsonl", [rec(50)])
        self.write("unmet/2026-10-18.jsonl", [rec(60)])
        self.assertEqual([d["t"] for d in decisions.recent(self.dir, "Luaprata", 9)], [1])

    def test_malformed_line_skipped(self):
        self.write("2026-10-18.jsonl", [json.dumps(rec(1)) + "\n", "{not json\n", "[1]\n", json.dumps(rec(2)) + "\n"],
                   raw=True)
        self.assertEqual([d["t"] for d in decisions.recent(self.dir, "Luaprata", 9)], [2, 1])

    def test_agent_dir_matched_case_insensitively(self):
        self.write("2026-10-18.jsonl", [rec(1)])
        self.assertEqual(len(decisions.recent(self.dir, "LUAPRATA", 9)), 1)
        self.assertEqual(decisions.recent(self.dir, "Nobody", 9), [])

    def test_big_file_is_read_only_from_its_tail(self):
        pad = json.dumps(rec(1, name="x" * 1000)) + "\n"
        n = decisions.MAX_BYTES // len(pad) + 5
        self.write("2026-10-18.jsonl", [pad] * n + [json.dumps(rec(99)) + "\n"], raw=True)
        got = decisions.recent(self.dir, "Luaprata", 10_000)
        self.assertEqual(got[0]["t"], 99)
        self.assertLess(len(got), n)   # the head was never read
        self.assertTrue(all(d["action"] == "x" * 1000 for d in got[1:]))   # the cut first line was dropped


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_decisions_503_without_audit_dir(self):
        for value in ("", "/nonexistent-326"):
            with mock.patch.object(state, "AUDIT_DIR", value):
                self.assertEqual(self.client.get("/api/v2/characters/A/decisions").status_code, 503)

    def test_decisions_route(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(state, "AUDIT_DIR", d):
            os.makedirs(os.path.join(d, "Aaa"))
            with open(os.path.join(d, "Aaa", "2026-10-18.jsonl"), "w") as f:
                f.write(json.dumps(rec(1)) + "\n" + json.dumps(rec(2, ok=False)) + "\n")
            r = self.client.get("/api/v2/characters/aaa/decisions?limit=1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["character"], len(body["decisions"])), ("aaa", 1))
        first = body["decisions"][0]
        self.assertEqual((first["t"], first["ok"], first["error"]), (2, False, "boom"))

    FETCHED = {"name": "Aaa", "level": 12, "class": 1, "class_name": "Warrior", "race_name": "Human",
               "class_color": "#C79C6E", "zone_name": "Elwynn Forest", "online": True, "health": 90,
               "max_health": 100, "money": 12345,
               "power": {"mana": 0, "rage": 40}, "max_power": {"mana": 0, "rage": 100}}

    def test_summary_built_from_fetch_character(self):
        with mock.patch.object(character, "fetch_character", return_value=self.FETCHED) as m:
            r = self.client.get("/api/v2/characters/Aaa")
        m.assert_called_once_with("Aaa")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["power_type"], "rage")
        self.assertEqual((r.json()["power"], r.json()["max_power"], r.json()["money"]), (40, 100, 12345))

    def test_summary_without_character_stats_row(self):
        with mock.patch.object(character, "fetch_character",
                               return_value=dict(self.FETCHED, max_health=None, max_power=None)):
            r = self.client.get("/api/v2/characters/Aaa")
        self.assertEqual((r.json()["max_health"], r.json()["max_power"]), (None, None))

    def test_unknown_character_404(self):
        with mock.patch.object(character, "fetch_character", return_value=None):
            r = self.client.get("/api/v2/characters/Nobody")
        self.assertEqual((r.status_code, r.json()), (404, {"error": "unknown character"}))


if __name__ == "__main__":
    unittest.main()
