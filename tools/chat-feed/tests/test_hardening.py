"""UM-48: /metrics, the stream token, the client caps and log rotation."""

import http.client
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
from app import ChatFeed, LogTailer  # noqa: E402

PUBLIC = {"say", "yell", "channel"}
app.LOG.setLevel("WARNING")      # the tailer logs every reopen
SAY = "2026-09-25 10:00:00 Player {} says (language 1): {}\n"


def say(sender="Rubens", text="hi", key=None):
    return {"kind": "say", "sender": sender, "text": text, "dedupe_key": key or text}


def metric_lines(text):
    return [line for line in text.splitlines() if not line.startswith("#")]


class MetricsTests(unittest.TestCase):
    def test_counts_events_by_kind_and_keeps_zero_series_for_public_kinds(self):
        feed = ChatFeed(10, PUBLIC)
        feed.publish_relay_event(say(text="one"))
        feed.publish_relay_event(say(text="two"))
        feed.publish_relay_event({"kind": "yell", "sender": "Rubens", "text": "HEY"})
        lines = metric_lines(feed.metrics())
        self.assertIn('chat_feed_events_total{kind="say"} 2', lines)
        self.assertIn('chat_feed_events_total{kind="yell"} 1', lines)
        self.assertIn('chat_feed_events_total{kind="channel"} 0', lines)

    def test_filtered_and_duplicate_events_are_not_counted_as_published(self):
        feed = ChatFeed(10, PUBLIC)
        feed.publish_relay_event(say())
        feed.publish_relay_event(say())                                    # duplicate
        feed.publish_relay_event({"kind": "whisper", "sender": "A", "text": "psst"})
        feed.publish_relay_event("garbage")
        lines = metric_lines(feed.metrics())
        self.assertIn('chat_feed_events_total{kind="say"} 1', lines)
        self.assertFalse([line for line in lines if "whisper" in line])
        self.assertIn("chat_feed_ingest_duplicates_total 1", lines)
        self.assertIn("chat_feed_ingest_rejected_total 1", lines)

    def test_parse_errors_and_last_event_timestamp(self):
        feed = ChatFeed(10, PUBLIC)
        self.assertIn("chat_feed_last_event_timestamp_seconds 0.0", metric_lines(feed.metrics()))
        feed.publish_line("Player Rubens does something new", "1:0")
        feed.publish_line(SAY.format("Rubens", "hi"), "1:1")
        lines = metric_lines(feed.metrics())
        self.assertIn("chat_feed_parse_errors_total 1", lines)
        stamp = [line for line in lines if line.startswith("chat_feed_last_event_timestamp_seconds")]
        self.assertGreater(float(stamp[0].split()[1]), 1_700_000_000)

    def test_every_sample_has_help_and_type_and_a_numeric_value(self):
        text = ChatFeed(10, PUBLIC).metrics()
        self.assertTrue(text.endswith("\n"))
        names = set()
        for line in metric_lines(text):
            name, value = line.rsplit(" ", 1)
            float(value)
            names.add(name.split("{")[0])
        for name in names:
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} ", text)
        self.assertEqual(names, {
            "chat_feed_events_total", "chat_feed_parse_errors_total",
            "chat_feed_ingest_duplicates_total", "chat_feed_ingest_rejected_total",
            "chat_feed_clients", "chat_feed_clients_rejected_total",
            "chat_feed_log_rotations_total", "chat_feed_last_event_timestamp_seconds"})

    def test_label_values_are_escaped(self):
        self.assertEqual(app._label('a"b\\c\nd'), 'a\\"b\\\\c\\nd')


class ClientCapTests(unittest.TestCase):
    def test_per_ip_and_total_caps(self):
        feed = ChatFeed(10, PUBLIC, max_clients=3, max_clients_per_ip=2)
        self.assertTrue(feed.add_client("10.0.0.1"))
        self.assertTrue(feed.add_client("10.0.0.1"))
        self.assertFalse(feed.add_client("10.0.0.1"))      # per-IP cap
        self.assertTrue(feed.add_client("10.0.0.2"))
        self.assertFalse(feed.add_client("10.0.0.3"))      # total cap
        self.assertEqual(feed.health()["clients"], 3)
        feed.remove_client("10.0.0.1")
        self.assertTrue(feed.add_client("10.0.0.3"))
        self.assertIn("chat_feed_clients_rejected_total 2", metric_lines(feed.metrics()))

    def test_zero_disables_the_caps(self):
        feed = ChatFeed(10, PUBLIC, max_clients=0, max_clients_per_ip=0)
        self.assertTrue(all(feed.add_client("10.0.0.1") for _ in range(50)))

    def test_removing_the_last_client_forgets_the_ip(self):
        feed = ChatFeed(10, PUBLIC)
        feed.add_client("10.0.0.1")
        feed.remove_client("10.0.0.1")
        self.assertEqual(dict(feed.clients), {})


class ServerTests(unittest.TestCase):
    """The real handler over a loopback socket."""

    def start(self, stream_token="", **feed_args):
        feed = ChatFeed(10, PUBLIC, **feed_args)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        httpd.daemon_threads = True
        httpd.feed, httpd.ingest_token, httpd.stream_token = feed, "", stream_token
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return feed, httpd.server_address[1]

    def get(self, port, path, headers=None):
        """(status, response, connection); the caller closes the connection."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers=headers or {})
        return conn.getresponse(), conn

    def test_metrics_endpoint_is_prometheus_text(self):
        feed, port = self.start()
        feed.publish_relay_event(say())
        response, _ = self.get(port, "/metrics")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/plain; version=0.0.4"))
        self.assertIn('chat_feed_events_total{kind="say"} 1', response.read().decode())

    def test_stream_is_open_without_a_token_configured(self):
        feed, port = self.start()
        feed.publish_relay_event(say(text="hello"))
        response, _ = self.get(port, "/api/chat/stream")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.fp.readline(), b"id: relay:1\n")

    def test_stream_token_is_required_when_configured(self):
        feed, port = self.start(stream_token="s3cret")
        feed.publish_relay_event(say())
        for path, headers in (("/api/chat/stream", None),
                              ("/api/chat/stream?token=wrong", None),
                              ("/api/chat/stream", {"Authorization": "Bearer wrong"}),
                              ("/api/chat/stream", {"Authorization": "Basic s3cret"})):
            response, _ = self.get(port, path, headers)
            self.assertEqual(response.status, 401, (path, headers))
            self.assertNotIn(b"relay", response.read())
        self.assertEqual(feed.health()["clients"], 0)

    def test_stream_accepts_the_token_as_bearer_or_query(self):
        feed, port = self.start(stream_token="s3cret")
        feed.publish_relay_event(say())
        for path, headers in (("/api/chat/stream?token=s3cret", None),
                              ("/api/chat/stream", {"Authorization": "Bearer s3cret"})):
            response, _ = self.get(port, path, headers)
            self.assertEqual(response.status, 200, (path, headers))
            self.assertEqual(response.fp.readline(), b"id: relay:1\n")

    def test_healthz_and_metrics_stay_open_with_a_stream_token(self):
        _, port = self.start(stream_token="s3cret")
        for path in ("/healthz", "/metrics"):
            response, _ = self.get(port, path)
            self.assertEqual(response.status, 200, path)
            response.read()

    def test_client_cap_returns_429_and_frees_the_slot_on_disconnect(self):
        feed, port = self.start(max_clients=1, max_clients_per_ip=5)
        feed.publish_relay_event(say(text="one"))
        first, conn = self.get(port, "/api/chat/stream")
        self.assertEqual(first.status, 200)
        first.fp.readline()
        second, _ = self.get(port, "/api/chat/stream")
        self.assertEqual(second.status, 429)
        self.assertEqual(json.loads(second.read()), {"error": "too many stream clients"})

        # http.client only shuts the socket down when the response is closed too.
        first.close()
        conn.close()
        # The handler notices on its next write, which a new event triggers.
        for n in range(50):
            feed.publish_relay_event(say(text=f"wake {n}"))
            with feed.condition:
                if feed.condition.wait_for(lambda: not feed.clients, timeout=0.1):
                    break
        self.assertEqual(feed.health()["clients"], 0)
        third, _ = self.get(port, "/api/chat/stream")
        self.assertEqual(third.status, 200)


class RotationTests(unittest.TestCase):
    """The tailer against a real file: append, truncate (copytruncate) and
    replace (rename + create, what logrotate and Docker's json-file do)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "Server.log")
        self.feed = ChatFeed(50, PUBLIC)
        self.tailer = LogTailer(self.feed, self.path)
        self.addCleanup(self.tailer.close)

    def write(self, text, mode="a"):
        with open(self.path, mode, encoding="utf-8") as handle:
            handle.write(text)

    def drain(self):
        while self.tailer.poll():
            pass
        return [event["text"] for event in self.feed.snapshot_after("")]

    def test_missing_file_raises_until_it_appears(self):
        with self.assertRaises(FileNotFoundError):
            self.tailer.poll()
        self.write("")
        self.assertEqual(self.drain(), [])
        self.write(SAY.format("Rubens", "now"))
        self.assertEqual(self.drain(), ["now"])

    def test_existing_content_is_skipped_on_first_open_only(self):
        self.write(SAY.format("Rubens", "old"))
        self.assertEqual(self.drain(), [])
        self.write(SAY.format("Rubens", "new"))
        self.assertEqual(self.drain(), ["new"])
        self.assertEqual(self.feed.log_rotations, 0)

    def test_partial_line_waits_for_its_newline(self):
        self.write("")
        self.drain()
        self.write("2026-09-25 10:00:00 Player Rubens says (language 1): hel")
        self.assertEqual(self.drain(), [])
        self.write("lo\n")
        self.assertEqual(self.drain(), ["hello"])

    def test_truncation_is_read_from_the_start(self):
        self.write(SAY.format("Rubens", "before one") + SAY.format("Rubens", "before two"))
        self.drain()
        self.write(SAY.format("Rubens", "after"), mode="w")        # same inode, shorter
        self.assertEqual(self.drain(), ["after"])
        self.assertEqual(self.feed.log_rotations, 1)

    def test_inode_replacement_is_read_from_the_start(self):
        self.write("")
        self.drain()
        self.write(SAY.format("Rubens", "old file"))
        self.assertEqual(self.drain(), ["old file"])
        os.rename(self.path, self.path + ".1")
        self.write(SAY.format("Rubens", "new file one") + SAY.format("Rubens", "new file two"))
        self.assertEqual(self.drain(), ["old file", "new file one", "new file two"])
        self.assertEqual(self.feed.log_rotations, 1)

    def test_event_ids_differ_across_a_replacement(self):
        self.write("")
        self.drain()
        self.write(SAY.format("Rubens", "a"))
        self.drain()
        os.rename(self.path, self.path + ".1")
        self.write(SAY.format("Rubens", "b"))
        self.drain()
        ids = [event["id"] for event in self.feed.snapshot_after("")]
        self.assertEqual(len(set(ids)), 2)

    def test_file_removed_then_recreated(self):
        self.write("")
        self.drain()
        os.remove(self.path)
        with self.assertRaises(FileNotFoundError):
            self.tailer.poll()
        self.tailer.close()                      # what run() does on FileNotFoundError
        self.write(SAY.format("Rubens", "back"))
        self.assertEqual(self.drain(), ["back"])
        self.assertEqual(self.feed.log_rotations, 1)


if __name__ == "__main__":
    unittest.main()
