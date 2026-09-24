"""Unit tests for agent.llm: tool-catalog wrapping, message building, and
tool-call extraction/validation from OpenAI-compatible chat-completions
responses, with the HTTP layer mocked (no real network call)."""

import json
import unittest
from unittest import mock

from agent import llm


def chat_response(name="set_target", arguments=None, arguments_json=None, extra_calls=None):
    """Build a minimal OpenAI-compatible chat.completions response with one
    (or more) tool_calls."""
    if arguments_json is None:
        arguments_json = json.dumps(arguments if arguments is not None else {"guid": 42})
    calls = [{"id": "call_1", "type": "function",
              "function": {"name": name, "arguments": arguments_json}}]
    if extra_calls:
        calls.extend(extra_calls)
    return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}


class BuildToolsTest(unittest.TestCase):
    def test_wraps_each_schema_as_function_tool(self):
        catalog = [{"name": "face", "description": "turn", "parameters": {"type": "object"}}]
        tools = llm.build_tools(catalog)
        self.assertEqual(tools, [{"type": "function",
                                   "function": {"name": "face", "description": "turn",
                                                "parameters": {"type": "object"}}}])

    def test_empty_catalog_yields_empty_tools(self):
        self.assertEqual(llm.build_tools([]), [])


class BuildMessagesTest(unittest.TestCase):
    def test_includes_system_and_user_snapshot(self):
        snapshot = {"position": {"map": 0, "x": 1.0, "y": 2.0, "z": 3.0}, "nearby_units": []}
        messages = llm.build_messages(snapshot)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn('"map": 0', messages[1]["content"])

    def test_persona_appended_to_system_prompt(self):
        messages = llm.build_messages({}, persona="a cautious rogue")
        self.assertIn("a cautious rogue", messages[0]["content"])

    def test_no_persona_by_default(self):
        messages = llm.build_messages({})
        self.assertNotIn("Persona:", messages[0]["content"])


class ExtractToolCallTest(unittest.TestCase):
    def test_extracts_name_and_dict_params(self):
        name, params = llm._extract_tool_call(chat_response("set_target", {"guid": 7}))
        self.assertEqual(name, "set_target")
        self.assertEqual(params, {"guid": 7})

    def test_string_arguments_parsed_as_json(self):
        response = chat_response(arguments_json='{"guid": 99}')
        name, params = llm._extract_tool_call(response)
        self.assertEqual(params, {"guid": 99})

    def test_missing_arguments_defaults_to_empty_dict(self):
        response = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "leave_group"}}
        ]}}]}
        name, params = llm._extract_tool_call(response)
        self.assertEqual(name, "leave_group")
        self.assertEqual(params, {})

    def test_no_tool_calls_raises(self):
        response = {"choices": [{"message": {"content": "I choose to do nothing."}}]}
        with self.assertRaises(llm.LLMError):
            llm._extract_tool_call(response)

    def test_multiple_tool_calls_uses_first(self):
        extra = [{"function": {"name": "face", "arguments": "{}"}}]
        response = chat_response("set_target", {"guid": 1}, extra_calls=extra)
        name, params = llm._extract_tool_call(response)
        self.assertEqual(name, "set_target")

    def test_malformed_response_raises(self):
        with self.assertRaises(llm.LLMError):
            llm._extract_tool_call({"choices": []})
        with self.assertRaises(llm.LLMError):
            llm._extract_tool_call({})

    def test_non_json_arguments_raises(self):
        response = chat_response(arguments_json="not json")
        with self.assertRaises(llm.LLMError):
            llm._extract_tool_call(response)

    def test_non_object_arguments_raises(self):
        response = chat_response(arguments_json="42")
        with self.assertRaises(llm.LLMError):
            llm._extract_tool_call(response)


class LLMClientChooseActionTest(unittest.TestCase):
    def test_posts_expected_body_and_returns_tool_call(self):
        client = llm.LLMClient("https://free.example/v1", "test-model", api_key="secret")
        captured = {}

        def fake_post(self, path, body):
            captured["path"] = path
            captured["body"] = body
            return chat_response("set_target", {"guid": 5})

        with mock.patch.object(llm.LLMClient, "_post", fake_post):
            name, params = client.choose_action({"position": None}, [
                {"name": "set_target", "description": "d", "parameters": {"type": "object"}}
            ])

        self.assertEqual((name, params), ("set_target", {"guid": 5}))
        self.assertEqual(captured["path"], "/chat/completions")
        self.assertEqual(captured["body"]["model"], "test-model")
        self.assertEqual(captured["body"]["tool_choice"], "required")
        self.assertEqual(len(captured["body"]["tools"]), 1)
        self.assertEqual(captured["body"]["tools"][0]["function"]["name"], "set_target")

    def test_post_http_error_raises_llm_error(self):
        import urllib.error

        client = llm.LLMClient("https://free.example/v1", "test-model")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("url", 500, "err", {}, None)):
            with self.assertRaises(llm.LLMError):
                client.choose_action({}, [])

    def test_post_mid_response_timeout_raises_llm_error_not_bare_timeout(self):
        # Regression test for UM-81: a socket timeout while reading the
        # response body (after the connection is already open) surfaces from
        # urlopen()'s context manager as a bare TimeoutError, not wrapped in
        # urllib.error.URLError. Simulate that by having the context manager
        # body (resp.read()) raise, since that's the phase urllib doesn't
        # wrap.
        client = llm.LLMClient("https://free.example/v1", "test-model")
        resp = mock.MagicMock()
        resp.read.side_effect = TimeoutError("timed out")
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(llm.LLMError):
                client.choose_action({}, [])

    def test_post_incomplete_read_raises_llm_error(self):
        # Regression test found in review: http.client.IncompleteRead (the
        # connection drops after headers but before the promised
        # Content-Length body arrives) is not an OSError subclass, so the
        # UM-81 fix's `except OSError` alone still missed it — same "crashes
        # the agent" failure mode as the bare TimeoutError case above.
        import http.client

        client = llm.LLMClient("https://free.example/v1", "test-model")
        resp = mock.MagicMock()
        resp.read.side_effect = http.client.IncompleteRead(b"partial")
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(llm.LLMError):
                client.choose_action({}, [])

    def test_base_url_trailing_slash_stripped(self):
        client = llm.LLMClient("https://free.example/v1/", "m")
        self.assertEqual(client.base_url, "https://free.example/v1")


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def unavailable(status=429, retry_after=None):
    return llm.LLMUnavailable(f"HTTP {status}", status=status, retry_after=retry_after,
                              retryable=status in (404, 429) or status >= 500)


class ScriptedPost:
    """Fake LLMClient._post: per-model queue of responses/exceptions; records
    the model of every request actually sent."""

    def __init__(self, script):
        self.script = {m: list(v) for m, v in script.items()}
        self.sent = []

    def __call__(self, path, body):
        self.sent.append(body["model"])
        outcome = self.script[body["model"]].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FallbackTest(unittest.TestCase):
    def make(self, models="a,b,c", **kw):
        clock = FakeClock()
        return llm.LLMClient("https://free.example/v1", models, clock=clock, **kw), clock

    def run_script(self, client, script):
        post = ScriptedPost(script)
        with mock.patch.object(client, "_post", post):
            try:
                return client.choose_action({}, []), post
            except llm.LLMError as e:
                return e, post

    def test_parse_model_list(self):
        self.assertEqual(llm.parse_model_list(" a, b,,a ,c "), ["a", "b", "c"])
        self.assertEqual(llm.parse_model_list("auto"), ["auto"])
        self.assertEqual(llm.parse_model_list(""), [])

    def test_single_model_is_backward_compatible(self):
        client, _ = self.make("test-model")
        self.assertEqual(client.models, ["test-model"])
        self.assertEqual(client.model, "test-model")

    def test_falls_back_in_order_on_429_and_5xx(self):
        client, _ = self.make()
        result, post = self.run_script(client, {
            "a": [unavailable(429)],
            "b": [unavailable(502)],
            "c": [chat_response("face", {})],
        })
        self.assertEqual(result, ("face", {}))
        self.assertEqual(post.sent, ["a", "b", "c"])
        self.assertEqual(client.last_model, "c")
        self.assertEqual(set(client.cooldowns()), {"a", "b"})

    def test_timeout_falls_back(self):
        client, _ = self.make("a,b")
        timeout = llm.LLMUnavailable("request failed: timed out")
        result, post = self.run_script(client, {"a": [timeout], "b": [chat_response()]})
        self.assertEqual(result[0], "set_target")
        self.assertEqual(post.sent, ["a", "b"])
        self.assertAlmostEqual(client.cooldowns()["a"], llm.DEFAULT_ERROR_COOLDOWN_S)

    def test_non_retryable_http_error_raises_without_fallback(self):
        client, _ = self.make("a,b")
        result, post = self.run_script(client, {"a": [unavailable(400)], "b": [chat_response()]})
        self.assertIsInstance(result, llm.LLMError)
        self.assertEqual(post.sent, ["a"])
        self.assertEqual(client.cooldowns(), {})

    def test_stale_model_404_falls_back_with_long_cooldown(self):
        client, _ = self.make("a,b")
        result, post = self.run_script(client, {"a": [unavailable(404)], "b": [chat_response()]})
        self.assertEqual(result[0], "set_target")
        self.assertEqual(post.sent, ["a", "b"])
        self.assertAlmostEqual(client.cooldowns()["a"], llm.MAX_COOLDOWN_S)

    def test_no_new_attempt_after_one_timeout_of_elapsed_time(self):
        client, clock = self.make("a,b,c", timeout=20.0)

        def slow_failure():
            clock.t += 20.0
            return unavailable(504)

        post = ScriptedPost({"a": [], "b": [chat_response()], "c": [chat_response()]})
        original = post.__call__

        def fake_post(path, body):
            if body["model"] == "a":
                post.sent.append("a")
                raise slow_failure()
            return original(path, body)

        with mock.patch.object(client, "_post", fake_post):
            with self.assertRaises(llm.LLMError):
                client.choose_action({}, [])
        self.assertEqual(post.sent, ["a"])

    def test_bad_tool_call_is_not_retried_on_next_model(self):
        client, _ = self.make("a,b")
        no_tool = {"choices": [{"message": {"content": "hi"}}]}
        result, post = self.run_script(client, {"a": [no_tool], "b": [chat_response()]})
        self.assertIsInstance(result, llm.LLMError)
        self.assertEqual(post.sent, ["a"])

    def test_attempts_bounded_per_call(self):
        client, _ = self.make("a,b,c,d", max_attempts=2)
        result, post = self.run_script(client, {
            "a": [unavailable(503)], "b": [unavailable(503)],
            "c": [chat_response()], "d": [chat_response()],
        })
        self.assertIsInstance(result, llm.LLMError)
        self.assertEqual(post.sent, ["a", "b"])
        # Next cycle goes straight to c, skipping the cooling models.
        result, post = self.run_script(client, {"c": [chat_response()]})
        self.assertEqual(post.sent, ["c"])

    def test_cooling_model_skipped_until_cooldown_expires(self):
        client, clock = self.make("a,b")
        self.run_script(client, {"a": [unavailable(429, retry_after=82)], "b": [chat_response()]})
        _, post = self.run_script(client, {"b": [chat_response()]})
        self.assertEqual(post.sent, ["b"])
        clock.t += 83
        _, post = self.run_script(client, {"a": [chat_response()]})
        self.assertEqual(post.sent, ["a"])

    def test_all_cooling_fails_fast_without_request(self):
        client, clock = self.make("a,b")
        self.run_script(client, {"a": [unavailable(429, retry_after=82)],
                                 "b": [unavailable(429, retry_after=40)]})
        clock.t += 10
        result, post = self.run_script(client, {})
        self.assertIsInstance(result, llm.LLMError)
        self.assertEqual(post.sent, [])
        self.assertIn("soonest b in 30s", str(result))

    def test_cooldown_default_and_clamping(self):
        client, _ = self.make("a,b,c")
        self.run_script(client, {"a": [unavailable(429)],
                                 "b": [unavailable(429, retry_after=99999)],
                                 "c": [unavailable(429, retry_after=0)]})
        cd = client.cooldowns()
        self.assertAlmostEqual(cd["a"], llm.DEFAULT_RATE_LIMIT_COOLDOWN_S)
        self.assertAlmostEqual(cd["b"], llm.MAX_COOLDOWN_S)
        self.assertAlmostEqual(cd["c"], llm.MIN_COOLDOWN_S)

    def test_routed_model_recorded_from_response(self):
        client, _ = self.make("auto")
        response = chat_response()
        response["model"] = "gemini-3.1-flash-lite"
        response["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
        self.run_script(client, {"auto": [response]})
        self.assertEqual(client.last_model, "gemini-3.1-flash-lite")
        self.assertEqual(client.last_usage["prompt_tokens"], 10)
        self.assertIsNotNone(client.last_latency_ms)

    def test_failed_call_clears_stale_usage(self):
        client, _ = self.make("a")
        response = chat_response()
        response["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
        self.run_script(client, {"a": [response]})
        self.assertEqual(client.last_usage["prompt_tokens"], 10)
        result, _ = self.run_script(client, {"a": [unavailable(502)]})
        self.assertIsInstance(result, llm.LLMError)
        self.assertEqual(client.last_usage, {})
        self.assertIsNone(client.last_latency_ms)
        self.assertIsNone(client.last_model)
        # And an all-cooling fast-fail clears them too.
        client.last_usage = {"prompt_tokens": 1}
        client.last_latency_ms = 5.0
        self.run_script(client, {})
        self.assertEqual(client.last_usage, {})
        self.assertIsNone(client.last_latency_ms)


class ThinkAuditFieldsTest(unittest.TestCase):
    """agent.think's _audit reads last_model/last_usage/last_latency_ms off
    the real client: a failed cycle after a good one must not repeat the
    good one's numbers (UM-94)."""

    def test_audit_records_routed_model_then_nothing_on_failure(self):
        from types import SimpleNamespace
        from agent import perception, think

        records = []
        audit = SimpleNamespace(record=lambda **kw: records.append(kw))
        client = llm.LLMClient("https://free.example/v1", "auto", clock=FakeClock())
        good = chat_response("no_such_action", {})
        good["model"] = "routed-model"
        good["usage"] = {"prompt_tokens": 100, "completion_tokens": 5}
        post = ScriptedPost({"auto": [good, unavailable(429, retry_after=60)]})
        session = SimpleNamespace(_send_packet=lambda *a: None)
        with mock.patch.object(client, "_post", post):
            think.think_and_act(session, perception.WorldState(), client, audit_logger=audit)
            think.think_and_act(session, perception.WorldState(), client, audit_logger=audit)

        self.assertEqual(records[0]["model"], "routed-model")
        self.assertEqual(records[0]["prompt_tokens"], 100)
        self.assertIsNotNone(records[0]["latency_ms"])
        self.assertIsNone(records[1]["model"])
        self.assertIsNone(records[1]["prompt_tokens"])
        self.assertIsNone(records[1]["latency_ms"])


class RetryAfterParsingTest(unittest.TestCase):
    BODY_429 = ('{"error":{"message":"All models exhausted: 1 route checked (1 rate-limited '
                'or on cooldown). Try again later. Soonest reset ~82s.",'
                '"type":"rate_limit_error","code":"rate_limit_exceeded"}}')

    def test_soonest_reset_hint(self):
        self.assertEqual(llm._retry_after_seconds(None, self.BODY_429), 82.0)

    def test_hint_units(self):
        self.assertEqual(llm._retry_after_seconds(None, "Soonest reset ~2m"), 120.0)
        self.assertEqual(llm._retry_after_seconds(None, "Soonest reset ~1500ms"), 1.5)

    def test_header_wins_over_hint(self):
        self.assertEqual(llm._retry_after_seconds("30", self.BODY_429), 30.0)

    def test_http_date_header(self):
        import email.utils
        import time as _time
        header = email.utils.formatdate(_time.time() + 120, usegmt=True)
        self.assertAlmostEqual(llm._retry_after_seconds(header, ""), 120, delta=2)

    def test_nothing_parseable(self):
        self.assertIsNone(llm._retry_after_seconds("soon", "All 1 routed attempt(s) failed"))
        self.assertIsNone(llm._retry_after_seconds(None, ""))

    def test_post_parses_429_via_urlopen(self):
        import io
        import urllib.error
        from email.message import Message

        client = llm.LLMClient("https://free.example/v1", "a,b", clock=FakeClock())
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(json.loads(req.data)["model"])
            raise urllib.error.HTTPError("url", 429, "Too Many Requests", Message(),
                                         io.BytesIO(self.BODY_429.encode()))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(llm.LLMError) as ctx:
                client.choose_action({}, [])
        self.assertEqual(calls, ["a", "b"])
        self.assertAlmostEqual(client.cooldowns()["a"], 82.0)
        self.assertIn("all models cooling down", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
