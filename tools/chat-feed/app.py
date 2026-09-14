#!/usr/bin/env python3
"""Small, dependency-free TrinityCore chat log tailer and SSE endpoint."""
import collections
import datetime as dt
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("chat-feed")

LOG_PATH = os.environ.get("LOG_PATH", "/logs/Server.log")
REPLAY_BUFFER_SIZE = int(os.environ.get("REPLAY_BUFFER_SIZE", "200"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9500"))
PUBLIC_KINDS = frozenset(
    kind.strip().lower()
    for kind in os.environ.get("CHAT_FEED_CHANNELS", "say,yell,channel").split(",")
    if kind.strip()
)

# Match the payload, not the appender-owned timestamp/logger prefix.  The caller
# first isolates the "Player ..." portion and these expressions must consume it.
SAY_RE = re.compile(r"^Player (?P<sender>.+?) says \(language \d+\): (?P<text>.*)$")
YELL_RE = re.compile(r"^Player (?P<sender>.+?) yells \(language \d+\): (?P<text>.*)$")
CHANNEL_RE = re.compile(
    r"^Player (?P<sender>.+?) tells channel (?P<channel>.+?): (?P<text>.*)$"
)
GROUP_RE = re.compile(
    r"^Player (?P<sender>.+?) tells (?P<kind>guild|party|raid|officer|battleground)(?: (?P<channel>.+?))?: (?P<text>.*)$"
)
WHISPER_RE = re.compile(
    r"^Player (?P<sender>.+?) whispers to (?P<channel>.+?): (?P<text>.*)$"
)


def now_iso8601():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_chat_line(line, event_id="", at=None):
    """Return a normalized event for a ChatLogScript candidate, or None.

    Prefixes vary with the configured appender, so only the stable payload after
    its first "Player " is parsed.  Text is captured after a known delimiter,
    preserving colons (and links) in message bodies.
    """
    marker = line.find("Player ")
    if marker < 0:
        return None
    payload = line[marker:].rstrip("\r\n")
    event = {"id": event_id, "at": at or now_iso8601(), "channel": None}
    for kind, pattern in (("say", SAY_RE), ("yell", YELL_RE), ("channel", CHANNEL_RE),
                          (None, GROUP_RE), ("whisper", WHISPER_RE)):
        match = pattern.fullmatch(payload)
        if match:
            values = match.groupdict()
            event.update(kind=values.get("kind") or kind, sender=values["sender"],
                         channel=values.get("channel"), text=values["text"])
            return event

    # Do not expose the raw unrecognized payload: it could be a new private-chat
    # shape.  The event and counter still make parser drift visible to consumers.
    event.update(kind="unknown", sender=None, text="Unrecognized chat payload")
    return event


class ChatFeed:
    def __init__(self, capacity, public_kinds):
        self.events = collections.deque(maxlen=capacity)
        self.public_kinds = public_kinds
        self.parse_errors = 0
        self.chat_candidates = 0
        self.condition = threading.Condition()

    def publish_line(self, line, event_id):
        event = parse_chat_line(line, event_id)
        if event is None:
            return
        with self.condition:
            self.chat_candidates += 1
            if event["kind"] == "unknown":
                self.parse_errors += 1
            elif event["kind"] not in self.public_kinds:
                return
            self.events.append(event)
            self.condition.notify_all()

    def snapshot_after(self, last_id):
        with self.condition:
            items = list(self.events)
            if last_id:
                for index, event in enumerate(items):
                    if event["id"] == last_id:
                        return items[index + 1:]
            return items

    def wait_after(self, last_id, timeout=15):
        with self.condition:
            self.condition.wait_for(
                lambda: bool(self.events) and self.events[-1]["id"] != last_id,
                timeout=timeout,
            )
        return self.snapshot_after(last_id)

    def health(self):
        with self.condition:
            return {"ok": True, "events": len(self.events),
                    "chat_candidates": self.chat_candidates,
                    "parse_errors": self.parse_errors}


def tail_log(feed, path):
    """Follow a path like tail -F, handling replacement and truncation."""
    handle = None
    inode = None
    position = 0
    first_open = True
    while True:
        try:
            path_stat = os.stat(path)
            if handle is None or inode != (path_stat.st_dev, path_stat.st_ino) or path_stat.st_size < position:
                if handle:
                    handle.close()
                handle = open(path, "r", encoding="utf-8", errors="replace")
                inode = (path_stat.st_dev, path_stat.st_ino)
                handle.seek(0, os.SEEK_END if first_open else os.SEEK_SET)
                position = handle.tell()
                first_open = False
                LOG.info("following %s (inode=%s)", path, inode)
            line_start = position
            line = handle.readline()
            if line:
                position = handle.tell()
                feed.publish_line(line, f"{inode[1]}:{line_start}")
            else:
                time.sleep(0.25)
        except FileNotFoundError:
            if handle:
                handle.close()
                handle = None
            time.sleep(1)
        except Exception:  # noqa: BLE001 - keep the sidecar alive on transient I/O
            LOG.exception("log tail failure")
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "chat-feed/0.1"

    @property
    def feed(self):
        return self.server.feed

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_event(self, event):
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"id: {event['id']}\nevent: chat\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, self.feed.health())
        if path != "/api/chat/stream":
            return self.send_json(404, {"error": "not found"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_id = self.headers.get("Last-Event-ID", "")
        try:
            while True:
                events = self.feed.snapshot_after(last_id)
                if events:
                    for event in events:
                        self.send_event(event)
                        last_id = event["id"]
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                self.feed.wait_after(last_id)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    feed = ChatFeed(REPLAY_BUFFER_SIZE, PUBLIC_KINDS)
    threading.Thread(target=tail_log, args=(feed, LOG_PATH), daemon=True).start()
    httpd = ThreadingHTTPServer(("", LISTEN_PORT), Handler)
    httpd.feed = feed
    LOG.info("serving SSE on :%s; public kinds: %s", LISTEN_PORT, ",".join(sorted(PUBLIC_KINDS)))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
