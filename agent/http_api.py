#!/usr/bin/env python3
"""Read-only observability API for one agent (UM-50).

An optional stdlib ThreadingHTTPServer, started only when AGENT_HTTP_PORT is
set (off by default). Every endpoint is GET and returns JSON; any other
method gets 405. There is no write or control endpoint on purpose: actions
stay inside the agent loop.

The single exception (#178) is `POST /control/walk`, which exists only when
AGENT_CONTROL_TOKEN is set (bearer-authenticated, called by tools/agent-runner,
see agent/control.py). With no token configured every POST is still a 405.

    GET /healthz     liveness, agent name, whether a session is attached
    GET /state       own stats, spellbook, quest log, equipment, inventory
    GET /perception  WorldState.snapshot(), the same view the LLM gets
    GET /brain       goal, brain (jev/llm), model, last decisions (from the audit records),
                     recent-action history, reflexes, token usage
    GET /events      Server-Sent Events: session.events + decisions
    POST /control/walk   operator walk, only with AGENT_CONTROL_TOKEN set (agent/control.py)

Threading: the recv thread and the think loop mutate state; handlers only
read it, through the same locked getters the think loop uses
(WorldState.snapshot(), build_quest_log(), ...) or by copying a container
first (_copy). A handler never takes a lock the game loop holds for long,
and the game loop never waits on a handler.

Secrets: nothing here reads Config.password or Config.llm_api_key, and every
response passes through agent.audit._redact as a second line of defence.
"""

import hmac
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import control
from . import spells as sp
from . import update_fields as uf
from .audit import _redact

log = logging.getLogger("agent.http")

# #264: bump with agent/api_schema.json (a test pins them together); breaking changes only.
API_VERSION = 1
DECISIONS_MAXLEN = 50     # decisions kept in memory for /brain and /events
DEFAULT_BRAIN_DECISIONS = 10
SSE_POLL_S = 0.5
SSE_KEEPALIVE_S = 15.0
MAX_CONTROL_BODY_BYTES = 2048
MAX_SSE_CLIENTS = 4       # each stream holds a thread; cap them


def _copy(fn, tries: int = 5):
    """Run fn() (e.g. list(some_deque)), retrying if another thread mutated
    the container mid-iteration ("deque mutated during iteration")."""
    for _ in range(tries - 1):
        try:
            return fn()
        except RuntimeError:
            time.sleep(0)
    return fn()


def _jsonable(obj):
    """Round-trip through json so int dict keys etc. come out as the client
    will see them, with secret-looking keys masked."""
    return _redact(json.loads(json.dumps(obj, default=str)))


class AgentObserver:
    """What the HTTP handlers read from. The main loop calls attach() when a
    session starts and detach() when it ends (sessions are rebuilt on every
    reconnect); the think loop's audit logger feeds record_decision()."""

    def __init__(self, agent_name: str, goal: str | None = None, model: str | None = None,
                 decisions_maxlen: int = DECISIONS_MAXLEN, brain: str | None = None):
        self.agent_name = agent_name
        self.goal = goal or None
        self.model = model or None
        self.brain_name = brain or None  # UM-101: "jev"/"llm", until a decision says otherwise
        self.started_at = time.time()
        self.session = None
        self.think_state = None
        self.reflex_state_fn = None
        self.decisions = deque(maxlen=decisions_maxlen)
        self._seq = 0
        self._lock = threading.Lock()  # guards _seq/token totals only
        self.tokens = {"prompt": 0, "completion": 0, "cycles": 0}
        # #178: held by the think loop for each cycle and by an operator walk for its
        # whole duration, so the two never drive the character at once.
        self.action_lock = threading.Lock()
        self.control_token = ""  # set by the main loop from AGENT_CONTROL_TOKEN; "" = no control endpoint

    # ── fed by the agent loop ────────────────────────────────────────
    def attach(self, session, think_state=None, reflex_state_fn=None):
        self.session = session
        self.think_state = think_state
        self.reflex_state_fn = reflex_state_fn

    def detach(self):
        self.session = None
        self.think_state = None
        self.reflex_state_fn = None

    def record_decision(self, record: dict):
        """Hook for agent.audit.AuditLogger.on_record: keep a compact copy
        of each think-cycle record (without the full snapshot)."""
        d = {k: v for k, v in record.items() if k != "snapshot"}
        with self._lock:
            self._seq += 1
            d["seq"] = self._seq
            self.tokens["cycles"] += 1
            self.tokens["prompt"] += record.get("prompt_tokens") or 0
            self.tokens["completion"] += record.get("completion_tokens") or 0
            self.decisions.append(d)

    def decisions_since(self, seq: int) -> list:
        return [d for d in _copy(lambda: list(self.decisions)) if d["seq"] > seq]

    # ── views ────────────────────────────────────────────────────────
    def healthz(self) -> dict:
        sess = self.session
        return {
            "ok": True,
            "agent": self.agent_name,
            "connected": sess is not None and not getattr(sess, "unexpected_disconnect", False),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def state(self) -> dict:
        sess = self.session
        if sess is None:
            return {"agent": self.agent_name, "connected": False}
        world = sess.world_state
        me = world.get_my_object()
        stats = {
            "name": getattr(sess, "player_name", "") or None,
            "guid": getattr(sess, "player_guid", None),
            "race": getattr(sess, "race", None),
            "class": getattr(sess, "class_", None),
            "level": None,
        }
        if me is not None:
            stats["level"] = me.level
            stats["health"] = me.health
            stats["max_health"] = me.max_health
            stats["power"] = dict(me.power or {})
            stats["max_power"] = dict(me.max_power or {})
            stats["is_dead"] = me.is_dead()
            stats["is_ghost"] = me.is_ghost()
            raw = dict(me.raw_fields or {})
            if uf.PLAYER_XP in raw:
                stats["xp"] = raw[uf.PLAYER_XP]
            if uf.PLAYER_NEXT_LEVEL_XP in raw:
                stats["next_level_xp"] = raw[uf.PLAYER_NEXT_LEVEL_XP]
        stats["money"] = getattr(sess, "coinage", None)
        pos = getattr(sess, "player_position", None)
        if pos:
            stats["position"] = {"map": pos[0], "x": pos[1], "y": pos[2], "z": pos[3]}
        spell_ids = sorted(_copy(lambda: list(getattr(sess, "spellbook", None) or ())))
        spellbook = []
        for sid in spell_ids:
            info = sp.get_spell_info(sid)
            spellbook.append({"id": sid, "name": info.name if info else None})
        equipment, inventory = world.build_equipment_and_inventory()
        return _jsonable({
            "agent": self.agent_name,
            "connected": True,
            "self": stats,
            "spellbook": spellbook,
            "quest_log": world.build_quest_log(),
            "equipment": equipment,
            "inventory": inventory,
        })

    def perception(self) -> dict:
        sess = self.session
        if sess is None:
            return {"agent": self.agent_name, "connected": False}
        chat = _copy(lambda: list(getattr(sess, "chat_inbox", None) or ()))
        snap = sess.world_state.snapshot(
            my_position=getattr(sess, "player_position", None),
            corpse_position=getattr(sess, "corpse_position", None),
            pending_invite=getattr(sess, "pending_invite", None),
            group=getattr(sess, "group", None),
            chat_inbox=chat)
        return _jsonable({"agent": self.agent_name, "connected": True, **snap})

    def brain(self, n: int = DEFAULT_BRAIN_DECISIONS) -> dict:
        decisions = _copy(lambda: list(self.decisions))
        n = max(0, min(n, len(decisions)))
        history = []
        ts = self.think_state
        if ts is not None:
            history = _copy(ts.for_prompt)
        reflexes = {}
        if self.reflex_state_fn is not None and self.session is not None:
            try:
                reflexes = self.reflex_state_fn(self.session)
            except Exception:  # best effort, like __main__._reflex_state
                reflexes = {}
        with self._lock:
            tokens = dict(self.tokens)
        last = decisions[-1] if decisions else None
        return _jsonable({
            "agent": self.agent_name,
            "connected": self.session is not None,
            "goal": (last or {}).get("goal") or self.goal,
            "brain": (last or {}).get("brain") or self.brain_name,
            "model": (last or {}).get("model") or self.model,
            "cycle": (last or {}).get("cycle"),
            "decisions": decisions[len(decisions) - n:] if n else [],
            "history": history,
            "reflexes": reflexes,
            "tokens": {
                "prompt_total": tokens["prompt"],
                "completion_total": tokens["completion"],
                "cycles": tokens["cycles"],
                "since": self.started_at,
            },
        })

    def events_since(self, last_t: float) -> list:
        sess = self.session
        if sess is None:
            return []
        evs = _copy(lambda: list(getattr(sess, "events", None) or ()))
        return [e for e in evs if e.get("t", 0) > last_t]


class _Handler(BaseHTTPRequestHandler):
    server_version = "wow-agent-observer/1"
    observer: AgentObserver = None  # set per server class in make_server()
    sse_slots: threading.BoundedSemaphore = None

    def log_message(self, fmt, *args):  # keep request noise out of INFO
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send_json(self, status: int, body: dict, extra_headers: dict | None = None):
        data = json.dumps({"api_version": API_VERSION, **body}, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _method_not_allowed(self):
        self._send_json(405, {"error": "read-only API: only GET is allowed"}, {"Allow": "GET"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = \
        _method_not_allowed

    def do_POST(self):
        url = urlsplit(self.path)
        obs = self.observer
        if not obs.control_token or url.path.rstrip("/") != "/control/walk":
            return self._method_not_allowed()
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode("utf-8"), obs.control_token.encode("utf-8")):
            return self._send_json(401, {"error": "control token required"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_CONTROL_BODY_BYTES:
            return self._send_json(400, {"ok": False, "outcome": "refused", "code": "bad_request",
                                         "error": "a JSON object body is required"})
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send_json(400, {"ok": False, "outcome": "refused", "code": "bad_request",
                                         "error": "body is not valid JSON"})
        dry = parse_qs(url.query).get("dry_run", [""])[0] in ("1", "true")
        status, result = control.walk(obs, body, dry_run=dry)
        log.info("operator walk from %s: %s -> %s %s", self.client_address[0],
                 json.dumps({k: body.get(k) for k in ("x", "y", "z", "near_player")}
                            if isinstance(body, dict) else None),
                 result.get("outcome"), "(dry run)" if dry else "")
        try:
            self._send_json(status, result)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        obs = self.observer
        try:
            if path == "/healthz":
                return self._send_json(200, obs.healthz())
            if path == "/state":
                return self._send_json(200, obs.state())
            if path == "/perception":
                return self._send_json(200, obs.perception())
            if path == "/brain":
                try:
                    n = int(parse_qs(url.query).get("n", [DEFAULT_BRAIN_DECISIONS])[0])
                except ValueError:
                    n = DEFAULT_BRAIN_DECISIONS
                return self._send_json(200, obs.brain(n))
            if path == "/events":
                return self._stream_events()
            if path == "/":
                return self._send_json(200, {"endpoints": ["/healthz", "/state", "/perception",
                                                           "/brain", "/events"]})
            return self._send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("observer endpoint %s failed", path)
            try:
                self._send_json(500, {"error": "internal error"})
            except OSError:
                pass

    def _stream_events(self):
        if not self.sse_slots.acquire(blocking=False):
            return self._send_json(503, {"error": "too many event streams"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            obs = self.observer
            # Start from "now": only new events and decisions are streamed.
            last_t = time.monotonic()
            with obs._lock:
                last_seq = obs._seq
            last_write = time.monotonic()
            while not self.server.stopping.is_set():
                out = []
                for e in obs.events_since(last_t):
                    last_t = max(last_t, e.get("t", 0))
                    out.append(("event", e))
                for d in obs.decisions_since(last_seq):
                    last_seq = max(last_seq, d["seq"])
                    out.append(("decision", d))
                for kind, payload in out:
                    data = json.dumps(_jsonable(payload), separators=(",", ":"))
                    self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
                if out:
                    self.wfile.flush()
                    last_write = time.monotonic()
                elif time.monotonic() - last_write >= SSE_KEEPALIVE_S:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                self.server.stopping.wait(SSE_POLL_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away
        finally:
            self.sse_slots.release()


class ObserverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler):
        self.stopping = threading.Event()
        super().__init__(addr, handler)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


def make_server(observer: AgentObserver, host: str, port: int) -> ObserverServer:
    handler = type("AgentObserverHandler", (_Handler,), {
        "observer": observer,
        "sse_slots": threading.BoundedSemaphore(MAX_SSE_CLIENTS),
    })
    return ObserverServer((host, port), handler)


def start_server(observer: AgentObserver, host: str, port: int) -> ObserverServer:
    """Bind and serve on a daemon thread. Returns the server (call
    .shutdown() to stop). Raises OSError if the port can't be bound."""
    srv = make_server(observer, host, port)
    t = threading.Thread(target=srv.serve_forever, name="agent-http", daemon=True)
    t.start()
    log.info("observability API on http://%s:%d (read-only)", host, srv.server_address[1])
    return srv
