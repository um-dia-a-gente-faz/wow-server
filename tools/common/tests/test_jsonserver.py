"""#263: the JSON HTTP handler base in tools/common/jsonserver.py, exercised over a
real HTTPServer on an ephemeral port."""

import http.client
import json
import pathlib
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from jsonserver import HttpError, JsonHandler  # noqa: E402

TOKEN = "s3cret-token"


class Echo(JsonHandler):
    MAX_BODY = 64
    token = ""

    def do_POST(self):  # noqa: N802
        try:
            if self.path == "/auth":
                self.check_bearer(self.token)
                return self.send_json(200, {"ok": True})
            return self.send_json(200, {"echo": self.read_json()})
        except HttpError as e:
            return self.send_error_json(e.status, e.message)


class Server(unittest.TestCase):
    token = ""

    def setUp(self):
        handler = type("H", (Echo,), {"token": self.token})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # shutdown() waits out one poll interval, so keep it short.
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type"), json.loads(resp.read() or b"null")


class ReadJson(Server):
    def test_object_body_is_returned(self):
        status, _, body = self.request("/echo", b'{"a": 1}', {"Content-Length": "8"})
        self.assertEqual((status, body), (200, {"echo": {"a": 1}}))

    def test_body_at_the_limit_is_accepted(self):
        payload = json.dumps({"x": "y" * (Echo.MAX_BODY - len('{"x": ""}'))}).encode()
        self.assertEqual(len(payload), Echo.MAX_BODY)
        status, _, _ = self.request("/echo", payload)
        self.assertEqual(status, 200)

    def test_body_over_the_limit_is_413(self):
        # A valid JSON object one byte over MAX_BODY: only the size check rejects it.
        payload = json.dumps({"x": "y" * (Echo.MAX_BODY - len('{"x": ""}') + 1)}).encode()
        status, ctype, body = self.request("/echo", payload)
        self.assertEqual(status, 413)
        self.assertEqual(ctype, "application/json")
        self.assertIn("error", body)

    def test_invalid_json_is_400(self):
        self.assertEqual(self.request("/echo", b"{nope")[0], 400)

    def test_invalid_utf8_is_400(self):
        self.assertEqual(self.request("/echo", b'{"a": "\xff"}')[0], 400)

    def test_non_object_json_is_400(self):
        self.assertEqual(self.request("/echo", b"[1, 2]")[0], 400)
        self.assertEqual(self.request("/echo", b"3")[0], 400)

    def test_non_numeric_content_length_is_400(self):
        status, _, body = self.request("/echo", b"{}", {"Content-Length": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_negative_content_length_is_400(self):
        # rfile.read(-1) would block until the client closes; it must not get that far.
        self.assertEqual(self.request("/echo", b"{}", {"Content-Length": "-1"})[0], 400)


class Responses(Server):
    def test_send_json_sets_type_and_body(self):
        class Handler(JsonHandler):
            def do_GET(self):  # noqa: N802
                self.send_json(201, {"n": 1})

        self.httpd.RequestHandlerClass = Handler
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        self.assertEqual(json.loads(resp.read()), {"n": 1})

    def test_send_error_json_shape(self):
        status, ctype, body = self.request("/echo", b"[]")
        self.assertEqual(status, 400)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(set(body), {"error"})
        self.assertIsInstance(body["error"], str)

    def test_http_error_carries_status_and_message(self):
        err = HttpError(401, "unauthorized")
        self.assertEqual((err.status, err.message), (401, "unauthorized"))
        self.assertEqual(str(err), "unauthorized")


class Bearer(Server):
    token = TOKEN

    def test_correct_token_passes(self):
        status, _, body = self.request("/auth", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual((status, body), (200, {"ok": True}))

    def test_missing_header_is_401(self):
        status, _, body = self.request("/auth")
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_wrong_token_is_401(self):
        self.assertEqual(self.request("/auth", headers={"Authorization": "Bearer nope"})[0], 401)

    def test_token_without_bearer_scheme_is_401(self):
        self.assertEqual(self.request("/auth", headers={"Authorization": TOKEN})[0], 401)

    def test_token_prefix_is_401(self):
        self.assertEqual(
            self.request("/auth", headers={"Authorization": f"Bearer {TOKEN[:-1]}"})[0], 401)


class BearerOff(Server):
    token = ""

    def test_empty_token_disables_the_check(self):
        self.assertEqual(self.request("/auth")[0], 200)


if __name__ == "__main__":
    unittest.main()
