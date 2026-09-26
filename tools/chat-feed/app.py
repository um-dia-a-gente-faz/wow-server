#!/usr/bin/env python3
"""Small, dependency-free chat feed: an SSE endpoint fed by agents relaying
`SMSG_MESSAGECHAT` (UM-47, `POST /api/chat/ingest`), plus the original
TrinityCore log tailer.

The tailer is kept because it costs nothing, but it finds no chat on this
server build: TrinityCore here never writes player chat to any log (see
docs/AGENT-DIRECTION.md → known findings). The agent relay is the real
source — `agent/chat_relay.py`.
"""
import collections
import datetime as dt
import hashlib
import hmac
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
    for kind in os.environ.get("CHAT_FEED_CHANNELS", "say,yell,channel,emote,text_emote,system").split(",")
    if kind.strip()
)
# Shared secret for POST /api/chat/ingest. Empty (the default) accepts any
# LAN client, matching the rest of this LAN-only stack; set it to require
# `Authorization: Bearer <token>` from the agents. Never logged.
INGEST_TOKEN = os.environ.get("CHAT_FEED_INGEST_TOKEN", "").strip()
# Several agents hear the same /say, so each relayed event carries a
# dedupe_key computed from the message alone; the first copy inside this
# window wins. Identical text from the same speaker inside the window is
# therefore also collapsed — that is the accepted trade-off.
DEDUPE_WINDOW_S = float(os.environ.get("CHAT_FEED_DEDUPE_WINDOW_S", "5"))
MAX_INGEST_BYTES = int(os.environ.get("CHAT_FEED_MAX_INGEST_BYTES", "65536"))
MAX_INGEST_EVENTS = 50
MAX_TEXT_LEN = 512      # 3.3.5a's own chat limit is 255 bytes
MAX_FIELD_LEN = 64      # speaker/channel/target/source names

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
    event = {"id": event_id, "at": at or now_iso8601(), "channel": None,
             "target": None, "source": None}
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


def _clean(value, limit=MAX_FIELD_LEN):
    """Trim one relayed string field, or None. Control characters go: the
    text is untrusted player input and ends up in an SSE frame, where a
    stray newline would forge an event boundary."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch == "\t" or ch >= " ")[:limit].strip()
    return value or None


def normalize_relay_event(raw, at=None):
    """Validate/normalize one event POSTed by an agent (agent/chat_relay.py's
    build_event output) into the same shape the tailer produces, or None if
    it is unusable.

    `id` is assigned by the feed, never by the sender, so one relay cannot
    overwrite another's entry in a consumer's Last-Event-ID bookkeeping.
    """
    if not isinstance(raw, dict):
        return None
    kind = _clean(raw.get("kind"), 32)
    text = _clean(raw.get("text"), MAX_TEXT_LEN)
    if not kind or text is None:
        return None
    sender = _clean(raw.get("sender"))
    channel = _clean(raw.get("channel"))
    target = _clean(raw.get("target"))
    key = _clean(raw.get("dedupe_key"), 64)
    if not key:
        guid = raw.get("sender_guid")
        who = str(guid) if guid else (sender or "")
        key = hashlib.sha1("\x1f".join(
            (kind, who, channel or "", target or "", text)).encode("utf-8")).hexdigest()[:16]
    return {"id": "", "at": _clean(raw.get("at"), 40) or (at or now_iso8601()),
            "kind": kind.lower(), "sender": sender, "channel": channel,
            "target": target, "text": text, "source": _clean(raw.get("source")),
            "dedupe_key": key}


class ChatFeed:
    def __init__(self, capacity, public_kinds, dedupe_window_s=DEDUPE_WINDOW_S,
                 clock=time.monotonic):
        self.events = collections.deque(maxlen=capacity)
        self.public_kinds = public_kinds
        self.parse_errors = 0
        self.chat_candidates = 0
        self.ingested = 0
        self.ingest_duplicates = 0
        self.ingest_rejected = 0
        self.dedupe_window_s = dedupe_window_s
        self._clock = clock
        self._recent = collections.OrderedDict()  # dedupe_key -> clock()
        self._sequence = 0
        self.condition = threading.Condition()

    def publish_line(self, line, event_id):
        event = parse_chat_line(line, event_id)
        if event is None:
            return
        with self.condition:
            self.chat_candidates += 1
            if event["kind"] == "unknown":
                self.parse_errors += 1
                return
            elif event["kind"] not in self.public_kinds:
                return
            self._append(event)

    def publish_relay_event(self, raw):
        """UM-47: accept one event relayed by an agent. Returns "published",
        "duplicate" (another agent already reported this message),
        "filtered" (a kind this feed doesn't publish, e.g. whispers) or
        "rejected" (unusable payload)."""
        event = normalize_relay_event(raw)
        if event is None:
            with self.condition:
                self.ingest_rejected += 1
            return "rejected"
        key = event.pop("dedupe_key")
        with self.condition:
            if self._seen_recently(key):
                self.ingest_duplicates += 1
                return "duplicate"
            if event["kind"] not in self.public_kinds:
                return "filtered"
            self._sequence += 1
            event["id"] = f"relay:{self._sequence}"
            self.ingested += 1
            self._append(event)
            return "published"

    def _seen_recently(self, key):
        """Called with self.condition held. Dedupe across agents: the first
        copy of a message wins for DEDUPE_WINDOW_S. Filtered kinds are
        recorded too, so a whisper isn't re-evaluated per listener."""
        now = self._clock()
        while self._recent:
            oldest_key, seen_at = next(iter(self._recent.items()))
            if now - seen_at < self.dedupe_window_s:
                break
            self._recent.popitem(last=False)
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    def _append(self, event):
        """Called with self.condition held."""
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
                    "parse_errors": self.parse_errors,
                    "ingested": self.ingested,
                    "ingest_duplicates": self.ingest_duplicates,
                    "ingest_rejected": self.ingest_rejected}


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
                if not line.endswith("\n"):
                    handle.seek(line_start)
                    time.sleep(0.25)
                    continue
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

    def send_cors_headers(self):
        # The viewer (tools/wowmap) is served from :9400 and this feed from
        # :9500, so a browser needs CORS to open the stream at all. Read-only
        # public chat on a LAN-only realm: any origin may read it.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Last-Event-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        token = getattr(self.server, "ingest_token", "")
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):], token)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/chat/ingest":
            return self.send_json(404, {"error": "not found"})
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_json(400, {"error": "bad content-length"})
        if length <= 0 or length > MAX_INGEST_BYTES:
            return self.send_json(413, {"error": "body too large or empty"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self.send_json(400, {"error": "invalid json"})

        events = body.get("events") if isinstance(body, dict) else body
        if isinstance(events, dict):
            events = [events]
        if not isinstance(events, list):
            return self.send_json(400, {"error": "expected an events list"})

        counts = collections.Counter(
            self.feed.publish_relay_event(event) for event in events[:MAX_INGEST_EVENTS]
        )
        return self.send_json(202, {"published": counts["published"],
                                    "duplicates": counts["duplicate"],
                                    "filtered": counts["filtered"],
                                    "rejected": counts["rejected"]})

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
        self.send_cors_headers()
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
    httpd.ingest_token = INGEST_TOKEN  # value never logged
    LOG.info("serving SSE on :%s; ingest auth: %s; public kinds: %s", LISTEN_PORT,
             "token" if INGEST_TOKEN else "open (LAN)", ",".join(sorted(PUBLIC_KINDS)))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
