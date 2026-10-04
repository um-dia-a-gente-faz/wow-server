"""Tests for agent.jev: criteria building, request shape, choice extraction
and error handling. Unit tests mock urlopen (pattern: test_llm.py); the
FakeDecisionsServer tests run the real urllib path against an in-process
HTTP server that answers in the documented Decisions API shape (a stand-in
until tools/jev-mock, UM-96, exists)."""

import http.server
import io
import json
import os
import random
import threading
import unittest
import urllib.error
from unittest import mock

from agent import config, jev

CANDIDATES = [
    {"action": "attack", "params": {"guid": 0x1234}},
    {"action": "loot", "params": {"guid": 0x5678}},
    {"action": "accept_quest", "params": {"quest_id": 42, "npc_guid": 7}},
]


def decisions_response(choice, probabilities=None, confidence=0.67, usage=None):
    answer = {"type": "choice", "choice": choice}
    if probabilities is not None:
        answer["probabilities"] = probabilities
    if confidence is not None:
        answer["confidence"] = confidence
    return {"model": "typesafe/jev-1.13", "answers": {jev.QUESTION: answer},
            "usage": usage if usage is not None else
            {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return mock.patch("urllib.request.urlopen", return_value=FakeResponse(raw))


class CriteriaTest(unittest.TestCase):
    def test_key_from_action_and_sorted_params(self):
        self.assertEqual(jev.candidate_key(CANDIDATES[2]),
                         "accept_quest(npc_guid=7,quest_id=42)")
        self.assertEqual(jev.candidate_key({"action": "stop", "params": {}}), "stop()")

    def test_explicit_id_wins(self):
        self.assertEqual(jev.candidate_key({"id": "c3", "action": "stop", "params": {}}), "c3")

    def test_description_prefers_label_then_json(self):
        self.assertEqual(jev.candidate_description(
            {"action": "loot", "params": {}, "label": "loot the boar"}), "loot the boar")
        self.assertEqual(jev.candidate_description(CANDIDATES[0]),
                         '{"action":"attack","params":{"guid":4660}}')

    def test_empty_candidates_raise(self):
        with self.assertRaises(jev.JevError):
            jev.build_criteria([])

    def test_duplicate_keys_raise(self):
        with self.assertRaises(jev.JevError):
            jev.build_criteria([CANDIDATES[0], dict(CANDIDATES[0])])

    def test_malformed_candidate_raises(self):
        for bad in ({"params": {}}, {"action": "x", "params": "guid=1"}, "attack"):
            with self.assertRaises(jev.JevError):
                jev.build_criteria([bad])


class RequestShapeTest(unittest.TestCase):
    def test_choice_question_over_all_candidates(self):
        by_key = jev.build_criteria(CANDIDATES)
        body = jev.build_request("typesafe/jev-1.13", {"me": {"level": 3}}, by_key,
                                 persona="a careful hunter", history=[{"action": "rest"}])
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], "typesafe/jev-1.13")
        self.assertEqual(body["state"], {"snapshot": {"me": {"level": 3}},
                                         "history": [{"action": "rest"}]})
        q = body["questions"][jev.QUESTION]
        self.assertEqual(q["type"], "choice")
        self.assertIn("a careful hunter", q["instructions"])
        self.assertEqual(list(q["criteria"]), list(by_key))

    def test_history_omitted_when_empty(self):
        body = jev.build_request("m", {}, jev.build_criteria(CANDIDATES))
        self.assertNotIn("history", body["state"])


class ChooseActionTest(unittest.TestCase):
    def setUp(self):
        self.client = jev.JevClient("http://jev.test/api/alpha/", "typesafe/jev-1.13",
                                    api_key="sk-test")

    def test_returns_chosen_candidate_and_audit_fields(self):
        probs = {"loot(guid=22136)": 0.8, "attack(guid=4660)": 0.2}
        with fake_urlopen(decisions_response("loot(guid=22136)", probs, 0.6)) as urlopen:
            name, params = self.client.choose_action({}, CANDIDATES)
        self.assertEqual((name, params), ("loot", {"guid": 0x5678}))
        self.assertEqual(self.client.last_choice, "loot(guid=22136)")
        self.assertEqual(self.client.last_confidence, 0.6)
        self.assertEqual(self.client.last_probabilities, probs)
        self.assertEqual(self.client.last_usage["input_tokens"], 476)
        self.assertIsNotNone(self.client.last_latency_ms)

        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://jev.test/api/alpha/decisions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-test")
        sent = json.loads(req.data)
        self.assertEqual(sent["questions"][jev.QUESTION]["type"], "choice")

    def test_returned_params_are_a_copy(self):
        with fake_urlopen(decisions_response("attack(guid=4660)")):
            _, params = self.client.choose_action({}, CANDIDATES)
        params["guid"] = 0
        self.assertEqual(CANDIDATES[0]["params"]["guid"], 0x1234)

    def test_optional_answer_fields_missing(self):
        with fake_urlopen(decisions_response("attack(guid=4660)", confidence=None, usage={})):
            self.assertEqual(self.client.choose_action({}, CANDIDATES)[0], "attack")
        self.assertIsNone(self.client.last_confidence)
        self.assertEqual(self.client.last_probabilities, {})

    def test_no_auth_header_without_key(self):
        client = jev.JevClient("http://jev.test", "m")
        with fake_urlopen(decisions_response("attack(guid=4660)")) as urlopen:
            client.choose_action({}, CANDIDATES)
        self.assertIsNone(urlopen.call_args[0][0].get_header("Authorization"))

    def test_single_candidate_skips_the_call(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            result = self.client.choose_action({}, [{"action": "stop", "params": {}}])
        urlopen.assert_not_called()
        self.assertEqual(result, ("stop", {}))
        self.assertEqual(self.client.last_confidence, 1.0)

    def test_choice_not_offered_raises(self):
        with fake_urlopen(decisions_response("cast_spell(spell_id=133)")):
            with self.assertRaises(jev.JevError):
                self.client.choose_action({}, CANDIDATES)

    def test_malformed_responses_raise(self):
        for payload in ({"answers": {}}, {"answers": {jev.QUESTION: {"type": "choice"}}},
                        {"error": "x"}, [1, 2], b"<html>bad gateway</html>"):
            with self.subTest(payload=payload), fake_urlopen(payload):
                with self.assertRaises(jev.JevError):
                    self.client.choose_action({}, CANDIDATES)

    def test_http_error_carries_status_and_message(self):
        body = json.dumps({"error": {"code": 402, "message": "Insufficient credits"}}).encode()
        err = urllib.error.HTTPError("http://jev.test/decisions", 402, "Payment Required",
                                     {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(jev.JevError) as cm:
                self.client.choose_action({}, CANDIDATES)
        self.assertEqual(cm.exception.status, 402)
        self.assertIn("Insufficient credits", str(cm.exception))
        self.assertNotIn("sk-test", str(cm.exception))

    def test_network_failures_raise_jev_error(self):
        for exc in (urllib.error.URLError("refused"), TimeoutError("read timed out")):
            with self.subTest(exc=exc), mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(jev.JevError) as cm:
                    self.client.choose_action({}, CANDIDATES)
                self.assertIsNone(cm.exception.status)

    def test_failure_clears_previous_audit_fields(self):
        with fake_urlopen(decisions_response("attack(guid=4660)")):
            self.client.choose_action({}, CANDIDATES)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(jev.JevError):
                self.client.choose_action({}, CANDIDATES)
        self.assertIsNone(self.client.last_choice)
        self.assertIsNone(self.client.last_confidence)


class FakeDecisionsHandler(http.server.BaseHTTPRequestHandler):
    """Answers POST /api/alpha/decisions like the documented API, choosing a
    random criteria key. Records the last request for assertions."""
    last_request = None
    last_auth = None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).last_request, type(self).last_auth = body, self.headers.get("Authorization")
        if self.path != "/api/alpha/decisions":
            return self._send(404, {"error": {"code": 404, "message": "not found"}})
        answers = {}
        for name, q in body["questions"].items():
            keys = list(q["criteria"])
            weights = [random.random() for _ in keys]
            total = sum(weights)
            probs = {k: w / total for k, w in zip(keys, weights)}
            answers[name] = {"type": "choice", "choice": random.choice(keys),
                             "probabilities": probs, "confidence": random.random()}
        self._send(200, {"model": body["model"], "answers": answers,
                         "usage": {"input_tokens": 100, "output_tokens": 0, "cost": 0.0}})

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class FakeDecisionsServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeDecisionsHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_round_trip_returns_an_offered_candidate(self):
        client = jev.JevClient(f"{self.base}/api/alpha", "~typesafe/jev-latest", api_key="k")
        offered = {(c["action"], json.dumps(c["params"], sort_keys=True)) for c in CANDIDATES}
        for _ in range(10):
            name, params = client.choose_action({"me": {"hp": 50}}, CANDIDATES)
            self.assertIn((name, json.dumps(params, sort_keys=True)), offered)
            self.assertIn(client.last_choice, client.last_probabilities)
        self.assertEqual(FakeDecisionsHandler.last_auth, "Bearer k")
        self.assertEqual(FakeDecisionsHandler.last_request["model"], "~typesafe/jev-latest")

    def test_http_error_from_server(self):
        client = jev.JevClient(f"{self.base}/wrong", "m")
        with self.assertRaises(jev.JevError) as cm:
            client.choose_action({}, CANDIDATES)
        self.assertEqual(cm.exception.status, 404)

    def test_connection_refused(self):
        client = jev.JevClient("http://127.0.0.1:1", "m", timeout=2)
        with self.assertRaises(jev.JevError):
            client.choose_action({}, CANDIDATES)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = config.Config()
        self.assertEqual(cfg.jev_base_url, "https://openrouter.ai/api/alpha")
        self.assertEqual(cfg.jev_model, "typesafe/jev-1.13")
        self.assertEqual(cfg.jev_api_key, "")
        self.assertEqual(cfg.redacted()["jev_api_key"], "(unset)")

    def test_openrouter_key_fallback_and_redaction(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=True):
            self.assertEqual(config.Config().jev_api_key, "or-key")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key",
                                          "JEV_API_KEY": "jev-key"}, clear=True):
            cfg = config.Config()
        self.assertEqual(cfg.jev_api_key, "jev-key")
        self.assertEqual(cfg.redacted()["jev_api_key"], "***")
        self.assertNotIn("jev-key", json.dumps(cfg.redacted()))


class EndpointUrlTest(unittest.TestCase):
    """GH-195: the URL actually requested, per provider configuration."""

    CANDS = [{"action": "idle", "params": {}}, {"action": "move", "params": {"x": 1}}]

    def requested_url(self, client):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return FakeResponse(json.dumps(decisions_response("idle()")).encode())

        with mock.patch.object(jev.urllib.request, "urlopen", fake_urlopen):
            client.choose_action({}, self.CANDS)
        return seen[0]

    def test_default_is_openrouter_decisions(self):
        self.assertEqual(self.requested_url(jev.JevClient()),
                         "https://openrouter.ai/api/alpha/decisions")

    def test_native_typesafe(self):
        c = jev.JevClient("https://api.typesafe.ai", "jev-latest", api_key="k",
                          path="/v1/systemone")
        self.assertEqual(self.requested_url(c), "https://api.typesafe.ai/v1/systemone")

    def test_slashes_are_normalised(self):
        c = jev.JevClient("https://api.typesafe.ai/", "m", path="v1/systemone")
        self.assertEqual(self.requested_url(c), "https://api.typesafe.ai/v1/systemone")

    def test_mock_keeps_default_path(self):
        c = jev.JevClient("http://jev-mock:8090/api/alpha")
        self.assertEqual(self.requested_url(c), "http://jev-mock:8090/api/alpha/decisions")

    def test_env_reaches_the_request_url(self):
        env = {"JEV_BASE_URL": "https://api.typesafe.ai", "JEV_PATH": "/v1/systemone",
               "JEV_MODEL": "jev-latest"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = config.Config()
        c = jev.JevClient(cfg.jev_base_url, model=cfg.jev_model, api_key=cfg.jev_api_key,
                          path=cfg.jev_path)
        self.assertEqual(self.requested_url(c), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(c.model, "jev-latest")

    def test_env_unset_or_empty_path_defaults(self):
        for env in ({}, {"JEV_PATH": ""}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(config.Config().jev_path, "/decisions")


if __name__ == "__main__":
    unittest.main()
