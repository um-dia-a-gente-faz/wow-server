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

    def test_base_url_trailing_slash_stripped(self):
        client = llm.LLMClient("https://free.example/v1/", "m")
        self.assertEqual(client.base_url, "https://free.example/v1")


if __name__ == "__main__":
    unittest.main()
