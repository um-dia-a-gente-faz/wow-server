#!/usr/bin/env python3
"""UM-47: relay what the agent hears on `SMSG_MESSAGECHAT` out of the game,
to `tools/chat-feed`'s ingest endpoint.

TrinityCore on this build never writes player chat to any log (see
docs/AGENT-DIRECTION.md → known findings), so tailing `Server.log` shows
nothing. An agent is already a protocol client receiving every chat packet
in its range, so it is the data source: `agent/session.py`'s chat handler
hands each parsed entry to `ChatRelay.submit()`, and a single background
thread POSTs it as JSON.

Design constraints:

- **Never block or break the recv loop.** `submit()` only puts on a bounded
  queue and returns; a full queue drops the message and bumps a counter.
  Every network error is swallowed (counted + throttled log), because a
  dead observability sidecar must not stop an agent from playing.
- **Speaker names are resolved late.** A plain (non-GM) `SMSG_MESSAGECHAT`
  carries no sender name, only a GUID, so the name comes from the session's
  name cache — which may need one `CMSG_NAME_QUERY` round trip first. The
  worker therefore waits up to `name_wait_s` per message for the cache to
  fill before sending, instead of publishing a nameless line.
- **The packet never names the listener.** Its `TargetGUID` is
  `Chat::Initialize`'s `receiver`, which TrinityCore fills with the speaker
  (verified live — see `agent/tests/fixtures/chat/README.md`), so the relay
  ignores it. The one direction that has to be reconstructed is
  `whisper_inform`, the echo an agent gets after whispering someone: there
  the *addressee* is in `sender_guid`, so the event is rewritten to
  "this agent → that player".
- **Several agents hear the same message.** Each event carries a
  `dedupe_key` derived from the message itself (never from the listener),
  so the feed can collapse the copies — see `tools/chat-feed/app.py`.

stdlib only (`urllib`, `queue`, `threading`), like the rest of `agent/`.
"""

import datetime as dt
import hashlib
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("agent.chat_relay")

DEFAULT_INGEST_PATH = "/api/chat/ingest"
DEFAULT_QUEUE_SIZE = 200
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_NAME_WAIT_S = 2.0
ERROR_LOG_INTERVAL_S = 60.0

# agent.session.CHAT_KIND_NAMES[CHAT_MSG_WHISPER_INFORM] — spelled out rather
# than imported, so the relay stays independent of the session module.
WHISPER_INFORM = "whisper_inform"

# The feed shows text a player typed. Cap it so a hostile/odd sender can't
# push an unbounded body at the sidecar; 3.3.5a's own chat limit is far
# below this (255 bytes), so this only ever truncates something abnormal.
MAX_TEXT_LEN = 512


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dedupe_key(kind: str, sender_guid: int, sender_name: str, channel, target, text: str) -> str:
    """Stable id for "the same thing said once", computed from the message
    only — two agents standing next to each other produce the same key for
    one `/say`, and the feed keeps the first copy.

    The GUID is the sender identity when we have it (every agent sees the
    same GUID; not every agent has resolved the name yet). Falls back to
    the name for server-side senders that have no GUID (system messages,
    NPCs addressed by name).
    """
    who = str(sender_guid) if sender_guid else (sender_name or "")
    raw = "\x1f".join((kind, who, channel or "", target or "", text))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_event(entry: dict, *, sender_name: str = "", target: str = "",
                source: str = "", at: str | None = None) -> dict:
    """Normalize one `agent.session` chat entry into a feed event.

    `entry` is what `agent.handlers.chat.handle_messagechat` appends to
    `session.chat_inbox`: kind/sender_guid/sender_name/channel/text, plus
    `target_name` when the packet carried one (an NPC emote aimed at
    somebody). `sender_name` and `target` override the entry's own values
    once the session's name cache has resolved the GUID — the usual case
    for player chat, where the packet itself has no names at all.

    The text stays **untrusted**: it is carried verbatim (only length-
    capped) and every consumer is responsible for escaping it — the feed
    never renders it, and the prompt marks it as untrusted input.
    """
    text = (entry.get("text") or "")[:MAX_TEXT_LEN]
    kind = entry.get("kind") or "unknown"
    sender_guid = int(entry.get("sender_guid") or 0)
    sender = sender_name or entry.get("sender_name") or ""
    target = target or entry.get("target_name") or ""
    channel = entry.get("channel") or None
    return {
        "at": at or _utc_now_iso(),
        "kind": kind,
        "sender": sender or None,
        "sender_guid": sender_guid or None,
        "channel": channel,
        "target": target or None,
        "text": text,
        "source": source or None,
        "dedupe_key": dedupe_key(kind, sender_guid, sender, channel, target, text),
    }


class ChatRelay:
    """Background POSTer for chat events. One per agent process.

    `url` is either the chat-feed base URL (`http://host:9500`) or the full
    ingest URL; `DEFAULT_INGEST_PATH` is appended when it is missing.
    Construct with an empty `url` to get an inert relay (`enabled` False),
    which is what every agent gets until `AGENT_CHAT_RELAY_URL` is set.
    """

    def __init__(self, url: str, *, agent_name: str = "", token: str = "",
                 timeout: float = DEFAULT_TIMEOUT_S, queue_size: int = DEFAULT_QUEUE_SIZE,
                 name_wait_s: float = DEFAULT_NAME_WAIT_S,
                 opener=None, clock=time.monotonic, sleep=time.sleep):
        self.url = _ingest_url(url)
        self.agent_name = agent_name
        self._token = token  # never logged — see CLAUDE.md
        self.timeout = timeout
        self.name_wait_s = name_wait_s
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._sleep = sleep
        self._thread = None
        self._stopping = threading.Event()
        self._last_error_log = 0.0
        # Counters, surfaced by stats() and the agent's metrics endpoint.
        self.sent = 0
        self.dropped = 0   # queue was full (sidecar down/slow, or a chat flood)
        self.failed = 0    # POST raised

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="chat-relay", daemon=True)
        self._thread.start()

    def submit(self, entry: dict, resolve_name=None):
        """Queue one chat entry. Called from the session's recv thread, so
        it never blocks and never raises.

        `resolve_name(guid) -> str | None` is the session's player-name
        lookup (`WorldState.resolve_player_name`); it is passed per call
        rather than held on the relay because a reconnect builds a brand
        new session and WorldState (see agent/__main__.py).
        """
        if not self.enabled:
            return
        try:
            self._queue.put_nowait((dict(entry), resolve_name, _utc_now_iso(), self._clock()))
        except queue.Full:
            self.dropped += 1

    def stop(self, timeout: float = 2.0):
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            self._thread = None

    def stats(self) -> dict:
        return {"sent": self.sent, "dropped": self.dropped, "failed": self.failed,
                "queued": self._queue.qsize()}

    # ── Internal ──────────────────────────────────────────────

    def _run(self):
        while not self._stopping.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self.publish(*item)
            except Exception:  # noqa: BLE001 — observability must not kill the agent
                self.failed += 1
                self._log_error_throttled()

    def publish(self, entry: dict, resolve_name, at: str, queued_at: float):
        """Resolve the speaker (bounded wait), build the event and POST it.
        Public so tests can drive one message without the thread."""
        who = self._resolve(entry, resolve_name, queued_at)
        target = entry.get("target_name") or ""
        if entry.get("kind") == WHISPER_INFORM:
            # The echo of a whisper this agent sent: the wire's "sender" is
            # the person it was sent to. Present it the way a reader expects.
            who, target = self.agent_name, who
        event = build_event(entry, sender_name=who, target=target,
                            source=self.agent_name, at=at)
        self._post(event)
        self.sent += 1

    def _resolve(self, entry: dict, resolve_name, queued_at: float) -> str:
        """Player chat carries a GUID, not a name. Give the name cache up to
        `name_wait_s` (measured from when the message was queued) to answer
        the query the session kicked off, then give up and send what we
        have — a line with a null speaker is better than no line."""
        sender_name = entry.get("sender_name") or ""
        sender_guid = int(entry.get("sender_guid") or 0)
        if resolve_name is None or sender_name or not sender_guid:
            return sender_name
        while not self._stopping.is_set():
            sender_name = resolve_name(sender_guid) or ""
            if sender_name or self._clock() - queued_at >= self.name_wait_s:
                break
            self._sleep(0.1)
        return sender_name

    def _post(self, event: dict):
        body = json.dumps({"events": [event]}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        with self._opener(request, timeout=self.timeout) as response:
            response.read()

    def _log_error_throttled(self):
        now = self._clock()
        if now - self._last_error_log < ERROR_LOG_INTERVAL_S:
            return
        self._last_error_log = now
        # The URL is LAN-internal and carries no credential (the token, when
        # set, travels in a header and is never logged).
        log.warning("chat relay POST to %s failing (%d failed, %d dropped so far)",
                    self.url, self.failed, self.dropped, exc_info=True)


def _ingest_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith(DEFAULT_INGEST_PATH):
        return url
    return url + DEFAULT_INGEST_PATH
