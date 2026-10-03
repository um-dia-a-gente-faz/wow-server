"""Tests for tools/jev-mock. The HTTP tests run the real server on an
ephemeral 127.0.0.1 port and call it the way agent.jev.JevClient does:
urllib POST to {JEV_BASE_URL}/decisions with a JSON body and bearer auth.
JevClientContractTest additionally drives the real JevClient when agent/jev.py
exists (UM-99, PR #105); until that lands it is skipped."""

import json
import pathlib
import random
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

REPO = ROOT.parents[1]

CRITERIA = {"attack(guid=4660)": "attack the boar",
            "loot(guid=22136)": "loot the corpse",
            "rest()": "sit and regenerate"}


def request_body(criteria=CRITERIA, **overrides):
    body = {"model": "typesafe/jev-1.13", "state": {"snapshot": {"me": {"hp": 50}}},
            "questions": {"next_action": {"type": "choice", "instructions": "Pick one.",
                                          "criteria": criteria}}}
    body.update(overrides)
    return body


class AnswerTest(unittest.TestCase):
    def test_choice_is_an_offered_key_and_the_most_likely(self):
        rng = random.Random(1)
        for _ in range(200):
            a = server.answer_choice(CRITERIA, rng)
            self.assertEqual(a["type"], "choice")
            self.assertIn(a["choice"], CRITERIA)
            self.assertEqual(set(a["probabilities"]), set(CRITERIA))
            self.assertAlmostEqual(sum(a["probabilities"].values()), 1.0, places=3)
            self.assertEqual(max(a["probabilities"].values()), a["probabilities"][a["choice"]])
            self.assertTrue(0.0 <= a["confidence"] <= 1.0)

    def test_choice_is_roughly_uniform(self):
        rng = random.Random(7)
        counts = dict.fromkeys(CRITERIA, 0)
        for _ in range(3000):
            counts[server.answer_choice(CRITERIA, rng)["choice"]] += 1
        for n in counts.values():
            self.assertGreater(n, 800)

    def test_single_option(self):
        a = server.answer_choice({"only": "x"}, random.Random(0))
        self.assertEqual((a["choice"], a["confidence"], a["probabilities"]),
                         ("only", 1.0, {"only": 1.0}))

    def test_response_shape(self):
        r = server.decide(request_body(), 400, random.Random(0))
        self.assertEqual(r["model"], "typesafe/jev-1.13")
        self.assertTrue(r["id"].startswith("gen-dec-"))
        self.assertEqual(set(r["answers"]), {"next_action"})
        self.assertEqual(set(r["usage"]), {"input_tokens", "output_tokens", "cost"})
        self.assertEqual(r["usage"]["input_tokens"], 100)

    def test_validation(self):
        bad = [[], {"state": {}, "questions": {}},
               request_body(model=""), request_body(state=5), request_body(questions={}),
               request_body(questions={"q": {"type": "noul", "instructions": "x",
                                             "criteria": {"true": "a", "false": "b"}}}),
               request_body(questions={"q": {"type": "choice", "criteria": {"a": "b"}}}),
               request_body(criteria={}), request_body(criteria=["a", "b"])]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(server.BadRequest):
                server.validate(body)


class ServerCase(unittest.TestCase):
    server_kwargs: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.server = server.make_server("127.0.0.1", 0, seed=42, **cls.server_kwargs)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/api/alpha"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body, path="/decisions", api_key="sk-test"):
        """Same request JevClient._post builds."""
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())


class HttpTest(ServerCase):
    def test_decisions_round_trip(self):
        status, r = self.post(request_body())
        self.assertEqual(status, 200)
        answer = r["answers"]["next_action"]
        self.assertIn(answer["choice"], CRITERIA)
        self.assertEqual(r["model"], "typesafe/jev-1.13")
        self.assertIn("input_tokens", r["usage"])

    def test_auth_is_optional_by_default(self):
        self.assertEqual(self.post(request_body(), api_key="")[0], 200)

    def test_bad_requests_get_openrouter_error_shape(self):
        for body in (b"not json", request_body(criteria={})):
            status, r = self.post(body)
            self.assertEqual(status, 400)
            self.assertEqual(r["error"]["code"], 400)
            self.assertTrue(r["error"]["message"])

    def test_unknown_path_is_404(self):
        status, r = self.post(request_body(), path="/chat/completions")
        self.assertEqual((status, r["error"]["code"]), (404, 404))

    def test_healthz(self):
        with urllib.request.urlopen(self.base.replace("/api/alpha", "/healthz"), timeout=5) as resp:
            self.assertEqual(json.loads(resp.read()), {"ok": True})


class ForcedStatusTest(ServerCase):
    server_kwargs = {"force_status": 402}

    def test_forced_402(self):
        status, r = self.post(request_body())
        self.assertEqual(status, 402)
        self.assertEqual(r["error"]["code"], 402)
        self.assertIn("Insufficient credits", r["error"]["message"])


class RequireAuthTest(ServerCase):
    server_kwargs = {"require_auth": True}

    def test_missing_key_is_401(self):
        self.assertEqual(self.post(request_body(), api_key="")[0], 401)
        self.assertEqual(self.post(request_body())[0], 200)


try:
    sys.path.insert(0, str(REPO))
    from agent import jev  # noqa: E402
except ImportError:
    jev = None


@unittest.skipIf(jev is None, "agent/jev.py not on this branch yet (UM-99, PR #105)")
class JevClientContractTest(ServerCase):
    CANDIDATES = [{"action": "attack", "params": {"guid": 0x1234}},
                  {"action": "loot", "params": {"guid": 0x5678}},
                  {"action": "rest", "params": {}}]

    def test_jev_client_gets_an_offered_candidate(self):
        client = jev.JevClient(self.base, "typesafe/jev-1.13", api_key="sk-test")
        offered = [(c["action"], c["params"]) for c in self.CANDIDATES]
        for _ in range(10):
            self.assertIn(client.choose_action({"me": {"hp": 50}}, self.CANDIDATES), offered)
            self.assertIn(client.last_choice, client.last_probabilities)
            self.assertIsNotNone(client.last_confidence)
            self.assertIn("input_tokens", client.last_usage)


@unittest.skipIf(jev is None, "agent/jev.py not on this branch yet (UM-99, PR #105)")
class JevClientErrorTest(ServerCase):
    server_kwargs = {"force_status": 429}

    def test_status_reaches_jev_error(self):
        client = jev.JevClient(self.base, "typesafe/jev-1.13", api_key="sk-test")
        with self.assertRaises(jev.JevError) as cm:
            client.choose_action({}, JevClientContractTest.CANDIDATES)
        self.assertEqual(cm.exception.status, 429)


try:
    from agent import actions as ac
    from agent import brain
    from agent import perception as per
    from agent.think import think_and_act
except ImportError:
    brain = None


class _Recording(ac.Action if brain else object):
    name = "test_action"
    description = "records the params it ran with"
    params = {"value": {"type": "integer"}}
    required = ()

    def __init__(self):
        self.ran = []

    def execute(self, session, world, **params):
        self.ran.append(params)
        return ac.ActionResult(ok=True, detail=params)


class _FallbackLLM:
    model = "fallback-llm"

    def __init__(self):
        self.calls = 0

    def choose_action(self, snapshot, catalog, persona="", history=None):
        self.calls += 1
        return "test_action", {"value": 99}


class _BrainSeamCase(ServerCase):
    """UM-101 end to end: think_and_act -> Brain -> real JevClient -> this mock."""

    CANDIDATES = [{"id": f"test_action:value={v}", "label": f"value {v}",
                   "action": "test_action", "params": {"value": v}} for v in (1, 2, 3)]

    def think(self, b):
        from types import SimpleNamespace
        from unittest import mock
        world = per.WorldState()
        sess = SimpleNamespace(_send_packet=lambda *a: None)
        action = _Recording()
        records = []
        audit = SimpleNamespace(record=lambda **kw: records.append(kw))
        with mock.patch("agent.brain.cand.generate", return_value=list(self.CANDIDATES)):
            result = think_and_act(sess, world, b, registry={"test_action": action},
                                   audit_logger=audit, cycle=1)
        return result, action, records[-1]


@unittest.skipIf(brain is None, "agent/brain.py not on this branch (UM-101)")
class BrainSeamJevTest(_BrainSeamCase):
    def test_jev_mock_decides_and_audit_has_confidence(self):
        llm = _FallbackLLM()
        b = brain.Brain(jev=jev.JevClient(self.base, "typesafe/jev-1.13"), llm=llm)
        result, action, rec = self.think(b)
        self.assertTrue(result.ok, result.error)
        self.assertIn(action.ran[0]["value"], (1, 2, 3))
        self.assertEqual(llm.calls, 0)
        self.assertEqual(rec["brain"], "jev")
        self.assertTrue(0.0 <= rec["confidence"] <= 1.0)
        self.assertEqual(rec["candidates"], 3)
        self.assertIsNotNone(rec["prompt_tokens"])


@unittest.skipIf(brain is None, "agent/brain.py not on this branch (UM-101)")
class BrainSeamFallbackTest(_BrainSeamCase):
    server_kwargs = {"force_status": 402}

    def test_402_from_mock_falls_back_to_llm(self):
        llm = _FallbackLLM()
        b = brain.Brain(jev=jev.JevClient(self.base, "typesafe/jev-1.13"), llm=llm)
        result, action, rec = self.think(b)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(action.ran, [{"value": 99}])
        self.assertEqual(rec["brain"], "llm")
        self.assertIn("HTTP 402", rec["fallback"])


if __name__ == "__main__":
    unittest.main()
