#!/usr/bin/env python3
"""Read-only observability API for one agent (UM-50).

An optional stdlib ThreadingHTTPServer, started only when AGENT_HTTP_PORT is
set (off by default). Every endpoint is GET and returns JSON; any other
method gets 405. There is no write or control endpoint on purpose: actions
stay inside the agent loop.

The exceptions (#178, #330) are the POST routes in agent/control.py (ROUTES), which exist
only when AGENT_CONTROL_TOKEN is set (bearer-authenticated, called by tools/agent-runner).
With no token configured every POST is still a 405.

    GET /healthz     liveness, agent name, whether a session is attached
    GET /state       own stats, spellbook, quest log, equipment, inventory; the connection
                     lifecycle (ADR 0015) when one is attached, also while not in game
    GET /perception  WorldState.snapshot(), the same view the LLM gets
    GET /brain       goal, brain (jev/llm), model, last decisions (from the audit records),
                     recent-action history, reflexes, token usage
    GET /events      Server-Sent Events: session.events + decisions
    GET /unmet-intents   the newest unmet-intent records (agent.unmet, #319): unknown action or idle-only candidates
    GET /metrics     Prometheus text: wow_agent_swallowed_errors_total{agent,where}, router counters,
                     wow_agent_reconnect_attempts_total{agent,reason},
                     wow_agent_fleet_limits_state/_age_seconds and wow_agent_fleet_blocked_cycles_total (agent.metrics)
    POST /control/walk   operator walk, only with AGENT_CONTROL_TOKEN set (agent/control.py)
    POST /control/pause, /resume, /override, /release, /stop_follow, /follow   operator controls (#330), same gate

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
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from . import control
from . import spells as sp
from .missions import DEFAULT_MISSION
from . import update_fields as uf
from .audit import _redact
from .metrics import render_fleet, render_reconnects, render_router, render_swallowed, swallowed
from .unmet import UnmetIntents

log = logging.getLogger("agent.http")

# #264: bump with agent/api_schema.json (a test pins them together); breaking changes only.
API_VERSION = 2   # #330: the control routes and /state's control field
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


def current_objective(quest_log) -> dict | None:
    """#315: the first incomplete objective of the first incomplete quest, in log order. An item objective
    has no counter (count None), so it counts as incomplete until the quest leaves the log."""
    for q in quest_log or []:
        if q.get("state_name") != "incomplete":
            continue
        for o in q.get("objectives") or []:
            if o.get("count") is None or o["count"] < o["needed"]:
                return {"quest_id": q["quest_id"], "title": q.get("title"), "text": o.get("text"),
                        "count": o.get("count"), "needed": o.get("needed")}
    return None


class LifecycleView(Protocol):
    """What the observer needs from agent.lifecycle.Lifecycle (#292): a thread-safe snapshot."""

    def snapshot(self) -> dict[str, object]: ...


class HealthView(Protocol):
    """What /metrics reads of the brain's FleetLimits: one attribute, an immutable tuple."""
    health: tuple[str, float | None]


class AgentObserver:
    """What the HTTP handlers read from. The main loop calls attach() when a
    session starts and detach() when it ends (sessions are rebuilt on every
    reconnect); the think loop's audit logger feeds record_decision()."""

    def __init__(self, agent_name: str, goal: str | None = None, model: str | None = None,
                 decisions_maxlen: int = DECISIONS_MAXLEN, brain: str | None = None,
                 mission: str = DEFAULT_MISSION):
        self.agent_name = agent_name
        self._mission = mission  # #314: what the agent is for; set once by the composition root
        self.mission_source: Callable[[], str] | None = None  # #316: the brain's MissionState.current, set by agent.root
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
        self.control_state = control.ControlState()  # #330: pause/override flags, read by the think and reflex threads
        # #292: the supervisor's agent.lifecycle.Lifecycle. None until the supervisor is wired:
        # /state then has no "lifecycle".
        self.lifecycle: LifecycleView | None = None
        # #675: the brain's fleet limits file, set by start_observer. /metrics reads only its
        # `health` (an immutable tuple); it never calls blocked(), so the file is read by the think thread alone.
        self.fleet: HealthView | None = None
        # #319: the agent.unmet.UnmetIntents set by the composition root; None = no records (intents: []).
        self.unmet: UnmetIntents | None = None

    def metrics_text(self) -> str:
        """The body of GET /metrics."""
        return (render_swallowed(self.agent_name) + render_router(self.agent_name)
                + render_reconnects(self.agent_name)
                + render_fleet(self.agent_name, self.fleet.health if self.fleet is not None else None, time.time()))

    @property
    def mission(self) -> str:
        """The mission in force: the brain's current mission once wired (#316), else the configured one."""
        return self.mission_source() if self.mission_source is not None else self._mission

    @mission.setter
    def mission(self, value: str) -> None:
        self._mission = value

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
    def _lifecycle(self) -> dict:
        """{"lifecycle": snapshot} when a lifecycle is attached, else {} (#292)."""
        lc = self.lifecycle
        return {} if lc is None else {"lifecycle": lc.snapshot()}

    def healthz(self) -> dict:
        sess = self.session
        return {
            "ok": True,
            "agent": self.agent_name,
            "connected": sess is not None and not getattr(sess, "unexpected_disconnect", False),
            "uptime_s": round(time.time() - self.started_at, 1),
            "mission": self.mission,
        }

    def state(self) -> dict:
        sess = self.session
        if sess is None:
            return {"agent": self.agent_name, "connected": False, "mission": self.mission,
                    "control": self.control_state.snapshot(), **self._lifecycle()}
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
            if getattr(sess, "zone_id", None) is not None:
                stats["position"]["zone"] = sess.zone_id   # #316: absent until SMSG_INIT_WORLD_STATES arrives
        spell_ids = sorted(_copy(lambda: list(getattr(sess, "spellbook", None) or ())))
        spellbook = []
        for sid in spell_ids:
            info = sp.get_spell_info(sid)
            spellbook.append({"id": sid, "name": info.name if info else None})
        equipment, inventory = world.build_equipment_and_inventory()
        quest_log = world.build_quest_log()
        return _jsonable({
            "agent": self.agent_name,
            "connected": True,
            "mission": self.mission,
            "self": stats,
            "spellbook": spellbook,
            "quest_log": quest_log,
            "current_objective": current_objective(quest_log),
            "equipment": equipment,
            "inventory": inventory,
            "control": self.control_state.snapshot(),
            **self._lifecycle(),
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
            chat_inbox=chat, zone=getattr(sess, "zone_id", None))
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
            except Exception:  # best effort, like runloop.reflex_state
                swallowed("http.reflex_state", log, level=logging.WARNING)
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

    def unmet_intents(self) -> dict:
        """GET /unmet-intents (#319): the newest unmet-intent records, oldest first."""
        return _jsonable({"agent": self.agent_name,
                          "intents": self.unmet.recent() if self.unmet is not None else []})

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
        route = control.ROUTES.get(url.path.rstrip("/"))
        if not obs.control_token or route is None:
            return self._method_not_allowed()
        handler, body_optional = route
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode("utf-8"), obs.control_token.encode("utf-8")):
            return self._send_json(401, {"error": "control token required"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if body_optional and length == 0:
            body = {}
        elif not 0 < length <= MAX_CONTROL_BODY_BYTES:
            return self._send_json(400, {"ok": False, "outcome": "refused", "code": "bad_request",
                                         "error": "a JSON object body is required"})
        else:
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self._send_json(400, {"ok": False, "outcome": "refused", "code": "bad_request",
                                             "error": "body is not valid JSON"})
        dry = parse_qs(url.query).get("dry_run", [""])[0] in ("1", "true")
        status, result = handler(obs, body, dry)
        log.info("operator %s from %s: %s -> %s %s", url.path, self.client_address[0],
                 json.dumps({k: body.get(k) for k in ("x", "y", "z", "near_player", "player")}
                            if isinstance(body, dict) else None),
                 result.get("outcome") or result.get("code") or status, "(dry run)" if dry else "")
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
            if path == "/unmet-intents":
                return self._send_json(200, obs.unmet_intents())
            if path == "/metrics":   # Prometheus text, not JSON; the swallowed-error counter (#306)
                data = obs.metrics_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/":
                return self._send_json(200, {"endpoints": ["/healthz", "/state", "/perception",
                                                           "/brain", "/events", "/unmet-intents"]})
            return self._send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            swallowed("http.endpoint", log, path)
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
                    self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
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
        self._state_lock = threading.Lock()
        self._serving = False   # the serve loop is running
        self._closed = False    # shutdown() has run
        super().__init__(addr, handler)

    def serve_forever(self, poll_interval=0.5):
        with self._state_lock:
            if self._closed:   # shutdown() came first: never enter the loop on a closed socket
                return
            self._serving = True
        try:
            super().serve_forever(poll_interval)
        finally:
            with self._state_lock:
                self._serving = False

    def shutdown(self):
        self.stopping.set()
        with self._state_lock:
            self._closed = True
            serving = self._serving
        # BaseServer.shutdown() waits for the serve loop to exit, so it is only called when one
        # is running; with none it would block forever. A serve_forever() that starts later sees
        # _closed and returns at once.
        if serving:
            super().shutdown()
        self.server_close()   # after the loop has stopped; closing twice is harmless


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
