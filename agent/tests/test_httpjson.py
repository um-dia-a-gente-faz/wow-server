"""agent.httpjson (#454): the shared POST-JSON transport. No network, no sleeping."""
import io
import json
import logging
import unittest
import urllib.error
from unittest import mock

from agent import httpjson

FAKE_KEY = "sk-fake-0123456789abcdef"   # obviously not a real key
URL = "https://api.example.test/v1/decisions"


class Resp:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b"", headers=None, url=URL):
    return urllib.error.HTTPError(url, code, "err", headers or {}, io.BytesIO(body))


def urlopen(*results):
    """urlopen stand-in returning/raising each result in turn."""
    seq = list(results)

    def fake(req, timeout=None):
        fake.calls.append((req, timeout))
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    fake.calls = []
    return fake


class PostJsonTest(unittest.TestCase):
    def post(self, *results, **kw):
        fake = urlopen(*results)
        with mock.patch("urllib.request.urlopen", fake):
            try:
                return httpjson.post_json(URL, {"a": 1}, api_key=FAKE_KEY, **kw), fake
            except httpjson.HttpJsonError as e:
                e.calls = fake.calls
                raise

    def test_success_sends_json_bearer_and_timeout(self):
        out, fake = self.post(Resp(b'{"ok": true}'), timeout=7.5)
        self.assertEqual(out, {"ok": True})
        req, timeout = fake.calls[0]
        self.assertEqual(timeout, 7.5)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"a": 1})
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_no_key_no_authorization_header(self):
        fake = urlopen(Resp(b"{}"))
        with mock.patch("urllib.request.urlopen", fake):
            httpjson.post_json(URL, {})
        self.assertIsNone(fake.calls[0][0].get_header("Authorization"))

    def test_timeout_is_a_network_error(self):
        for exc in (TimeoutError("timed out"), urllib.error.URLError("refused")):
            with self.subTest(exc=exc), self.assertRaises(httpjson.HttpJsonError) as cm:
                self.post(exc)
            self.assertEqual(cm.exception.kind, "network")
            self.assertIsNone(cm.exception.status)

    def test_timeout_while_reading_body(self):
        resp = mock.MagicMock()
        resp.read.side_effect = TimeoutError("read timed out")
        resp.__enter__.return_value = resp
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(resp)
        self.assertEqual(cm.exception.kind, "network")

    def test_non_json_body(self):
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(Resp(b"<html>oops</html>"))
        self.assertEqual(cm.exception.kind, "non_json")
        self.assertEqual(cm.exception.body, b"<html>oops</html>")

    def test_require_object(self):
        out, _ = self.post(Resp(b"[1]"))
        self.assertEqual(out, [1])
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(Resp(b"[1]"), require_object=True)
        self.assertEqual(cm.exception.kind, "non_json")

    def test_http_error_carries_status_body_and_retry_after(self):
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(429, b"slow down", {"Retry-After": "12"}))
        e = cm.exception
        self.assertEqual((e.kind, e.status, e.body, e.retry_after), ("http", 429, b"slow down", 12.0))

    def test_retry_after_from_body_hint(self):
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(429, b"Soonest reset ~82s"))
        self.assertEqual(cm.exception.retry_after, 82.0)

    def test_error_body_limit(self):
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(500, b"x" * 900), error_body_limit=500)
        self.assertEqual(len(cm.exception.body), 500)

    def test_retry_hint_inside_error_body_limit_is_honoured(self):
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(429, b"x" * 100 + b" Soonest reset ~82s"), error_body_limit=500)
        self.assertEqual(cm.exception.retry_after, 82.0)

    def test_retry_hint_beyond_error_body_limit_is_not_honoured(self):
        # The hint is read from the first error_body_limit raw bytes only (#505).
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(429, b"x" * 600 + b" Soonest reset ~82s"), error_body_limit=500)
        self.assertIsNone(cm.exception.retry_after)

    def test_no_retry_by_default(self):
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(503), http_error(503))
        self.assertEqual(len(cm.exception.calls), 1)

    def test_retry_honours_retry_after_without_sleeping_for_real(self):
        sleeps = []
        out, fake = self.post(http_error(429, b"", {"Retry-After": "3"}), Resp(b'{"n": 2}'),
                              retries=2, retry_statuses=(429,), backoff_s=1.0, sleep=sleeps.append)
        self.assertEqual(out, {"n": 2})
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(len(fake.calls), 2)

    def test_retry_uses_backoff_without_retry_after_and_caps_wait(self):
        sleeps = []
        self.post(http_error(503), http_error(429, b"", {"Retry-After": "9999"}), Resp(b"{}"),
                  retries=2, retry_statuses={429, 503}, backoff_s=0.5, max_wait_s=30, sleep=sleeps.append)
        self.assertEqual(sleeps, [0.5, 30.0])

    def test_retry_exhaustion_raises_last_error(self):
        sleeps = []
        with self.assertRaises(httpjson.HttpJsonError) as cm:
            self.post(http_error(503, b"a"), http_error(503, b"b"), http_error(503, b"c"),
                      retries=2, retry_statuses=(503,), sleep=sleeps.append)
        self.assertEqual(cm.exception.body, b"c")
        self.assertEqual(len(cm.exception.calls), 3)
        self.assertEqual(len(sleeps), 2)

    def test_other_status_and_network_errors_are_not_retried(self):
        for first in (http_error(400), urllib.error.URLError("down")):
            sleeps = []
            with self.subTest(first=first), self.assertRaises(httpjson.HttpJsonError) as cm:
                self.post(first, Resp(b"{}"), retries=3, retry_statuses=(503,), sleep=sleeps.append)
            self.assertEqual((len(cm.exception.calls), sleeps), (1, []))


class RedactionTest(unittest.TestCase):
    """A key sent in headers (or in the URL query) must not surface in a message,
    an attribute, a chained exception or a log line."""

    def surfaces(self, e: BaseException) -> str:
        parts = [str(e), repr(e), repr(vars(e))]
        # what a traceback would print: the cause, or the context unless suppressed
        nxt = lambda x: x.__cause__ or (None if x.__suppress_context__ else x.__context__)  # noqa: E731
        cause = nxt(e)
        while cause is not None:
            parts += [str(cause), repr(cause)]
            cause = nxt(cause)
        return "\n".join(parts)

    def run_case(self, make_exc, url=URL, **kw):
        with self.assertLogs("agent.httpjson", level=logging.DEBUG) as logs, \
                mock.patch("urllib.request.urlopen", urlopen(make_exc(), make_exc())):
            try:
                httpjson.post_json(url, {}, api_key=FAKE_KEY, retries=1, retry_statuses=(500,),
                                   sleep=lambda s: None, **kw)
            except httpjson.HttpJsonError as e:
                return self.surfaces(e) + "\n".join(logs.output)
        self.fail("no error")

    def test_key_echoed_in_http_error_body(self):
        text = self.run_case(lambda: http_error(500, f"bad key {FAKE_KEY}".encode()))
        self.assertNotIn(FAKE_KEY, text)

    def test_key_straddling_the_error_body_limit(self):
        # The cut must come after redaction: cutting first leaves a key prefix.
        body = b"p" * 480 + FAKE_KEY.encode() + b" tail"
        text = self.run_case(lambda: http_error(500, body), error_body_limit=500)
        for i in range(len(FAKE_KEY) - 7):
            self.assertNotIn(FAKE_KEY[i:i + 8], text)

    def test_error_body_is_cut_after_redaction(self):
        body = b"p" * 480 + FAKE_KEY.encode() + b"q" * 100
        with mock.patch("urllib.request.urlopen", urlopen(http_error(500, body))):
            with self.assertRaises(httpjson.HttpJsonError) as cm:
                httpjson.post_json(URL, {}, api_key=FAKE_KEY, error_body_limit=500)
        self.assertLessEqual(len(cm.exception.body), 500)

    def test_bearer_header_echoed_in_error_body(self):
        text = self.run_case(lambda: http_error(500, f"got Authorization: Bearer {FAKE_KEY}".encode()))
        self.assertNotIn(FAKE_KEY, text)

    def test_key_in_url_query(self):
        url = f"{URL}?key={FAKE_KEY}&alt=json"
        text = self.run_case(lambda: http_error(500, b"x", url=url), url=url)
        self.assertNotIn(FAKE_KEY, text)
        self.assertIn("alt=json", text)   # only the secret is hidden

    def test_key_in_url_query_on_network_error(self):
        url = f"{URL}?api_key={FAKE_KEY}"
        with mock.patch("urllib.request.urlopen",
                        urlopen(urllib.error.URLError(f"cannot reach {url}"))):
            with self.assertRaises(httpjson.HttpJsonError) as cm:
                httpjson.post_json(url, {}, api_key=FAKE_KEY)
        self.assertNotIn(FAKE_KEY, self.surfaces(cm.exception))

    def test_key_echoed_in_non_json_body(self):
        with mock.patch("urllib.request.urlopen", urlopen(Resp(f"hello {FAKE_KEY}".encode()))):
            with self.assertRaises(httpjson.HttpJsonError) as cm:
                httpjson.post_json(URL, {}, api_key=FAKE_KEY)
        self.assertNotIn(FAKE_KEY, self.surfaces(cm.exception))

    def test_redact_leaves_plain_text_alone(self):
        self.assertEqual(httpjson.redact("Missing bearer token", [FAKE_KEY]), "Missing bearer token")
        self.assertEqual(httpjson.redact("https://x.test/a?page=2"), "https://x.test/a?page=2")
