"""agent.pathing.PathClient (#312): the POST /path contract of tools/pathfinder, against a stub
sidecar on port 0 (http.server, stdlib)."""

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agent import pathing
from agent.pathing import PathClient, PathUnavailable


class StubSidecar:
    """tools/pathfinder's POST /path on a free port. `reply(request_dict) -> (status, body_bytes)`."""

    def __init__(self, reply):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                status, raw = reply(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def ok_reply(points):
    body = {"status": "ok", "points": [{"x": x, "y": y, "z": z} for x, y, z in points]}
    return 200, json.dumps(body).encode()


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class PathClientTest(unittest.TestCase):
    def serve(self, reply):
        stub = StubSidecar(reply)
        self.addCleanup(stub.close)
        return stub

    def test_ok_returns_the_points_and_posts_the_contract_body(self):
        stub = self.serve(lambda req: ok_reply([(0.0, 0.0, 1.0), (5.0, 0.0, 2.5)]))
        points = PathClient(stub.url).path(530, (0.0, 0.0, 1.0), (5.0, 0.0, 2.0))
        self.assertEqual(points, [(0.0, 0.0, 1.0), (5.0, 0.0, 2.5)])
        self.assertEqual(stub.requests, [{"map": 530, "from": {"x": 0.0, "y": 0.0, "z": 1.0},
                                          "to": {"x": 5.0, "y": 0.0, "z": 2.0}}])

    def test_no_path_and_no_map_answer_none(self):
        for status in ("no_path", "no_map"):
            stub = self.serve(lambda req, s=status: (200, json.dumps({"status": s, "points": []}).encode()))
            self.assertIsNone(PathClient(stub.url).path(1, (0, 0, 0), (1, 0, 0)), status)

    def test_connection_refused_raises_path_unavailable(self):
        with self.assertRaises(PathUnavailable) as cm:
            PathClient(closed_port_url()).path(1, (0, 0, 0), (1, 0, 0))
        self.assertTrue(cm.exception.reason)

    def test_http_500_raises_path_unavailable(self):
        stub = self.serve(lambda req: (500, b'{"error": "internal error"}'))
        with self.assertRaises(PathUnavailable):
            PathClient(stub.url).path(1, (0, 0, 0), (1, 0, 0))

    def test_malformed_json_and_unknown_status_raise_path_unavailable(self):
        for raw in (b"not json", b'{"status": "weird", "points": []}', b'{"points": []}',
                    b'{"status": "ok", "points": [{"x": 1}]}'):
            stub = self.serve(lambda req, r=raw: (200, r))
            with self.assertRaises(PathUnavailable, msg=raw):
                PathClient(stub.url).path(1, (0, 0, 0), (1, 0, 0))

    def test_non_finite_coordinates_are_malformed(self):
        # json.loads accepts NaN and Infinity literals; a NaN waypoint would make the mover's
        # arrival test never true, so the move would never end.
        for raw in (b'{"status": "ok", "points": [{"x": NaN, "y": 0, "z": 0}]}',
                    b'{"status": "ok", "points": [{"x": 0, "y": Infinity, "z": 0}]}',
                    b'{"status": "ok", "points": [{"x": 0, "y": 0, "z": -Infinity}]}'):
            stub = self.serve(lambda req, r=raw: (200, r))
            with self.assertRaises(PathUnavailable, msg=raw):
                PathClient(stub.url).path(1, (0, 0, 0), (1, 0, 0))

    def test_boolean_coordinates_are_malformed(self):
        stub = self.serve(lambda req: (200, b'{"status": "ok", "points": [{"x": true, "y": 0, "z": 0}]}'))
        with self.assertRaises(PathUnavailable):
            PathClient(stub.url).path(1, (0, 0, 0), (1, 0, 0))

    def test_never_blocks_longer_than_its_timeout(self):
        def slow(req):
            time.sleep(1.0)
            return ok_reply([])
        stub = self.serve(slow)
        started = time.monotonic()
        with self.assertRaises(PathUnavailable):
            PathClient(stub.url, timeout_s=0.2).path(1, (0, 0, 0), (1, 0, 0))
        self.assertLess(time.monotonic() - started, 0.9)

    def test_only_path_unavailable_escapes(self):
        # A failure the HTTP layer did not anticipate still surfaces as PathUnavailable.
        with mock.patch.object(pathing.urllib.request, "urlopen", side_effect=RuntimeError("boom")), \
                self.assertRaises(PathUnavailable):
            PathClient("http://127.0.0.1:1").path(1, (0, 0, 0), (1, 0, 0))


class PathClientConfigTest(unittest.TestCase):
    def test_pathfinder_url_defaults_to_empty_and_is_read_from_env(self):
        from agent.config import Config
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Config().pathfinder_url, "")
        with mock.patch.dict("os.environ", {"AGENT_PATHFINDER_URL": "http://pf:8770"}, clear=True):
            self.assertEqual(Config().pathfinder_url, "http://pf:8770")

    def test_build_session_gives_the_session_its_path_client(self):
        from agent import root
        from types import SimpleNamespace
        agent = root.Agent(cfg=SimpleNamespace(pathfinder_url="http://pf:8770"), log=mock.Mock(),
                           chat_relay=mock.Mock(), brain=None, audit_logger=mock.Mock(), observer=None,
                           http_server=None, lifecycle=mock.Mock())
        sess = SimpleNamespace()
        with mock.patch.object(root, "connect_and_login", return_value=sess):
            self.assertIs(agent.build_session(), sess)
        self.assertIsInstance(sess.path_client, PathClient)

    def test_root_builds_one_client_only_when_the_url_is_set(self):
        from agent import root
        from types import SimpleNamespace
        self.assertIsNone(root.build_path_client(SimpleNamespace(pathfinder_url="")))
        client = root.build_path_client(SimpleNamespace(pathfinder_url="http://pf:8770"))
        self.assertIsInstance(client, PathClient)


if __name__ == "__main__":
    unittest.main()
