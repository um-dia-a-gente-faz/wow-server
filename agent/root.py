"""Composition root (#292): build_agent(cfg) wires one agent (chat relay, brain, audit log,
observer, lifecycle) and Agent.run() supervises its connection. agent/__main__.py parses
arguments and calls these; anything else that needs a whole agent can call them too."""

import logging
import threading
import time
from dataclasses import dataclass

from . import spells
from .audit import AuditLogger
from .brain import Brain
from .chat_relay import ChatRelay
from .http_api import AgentObserver, ObserverServer, start_server
from .lifecycle import Lifecycle
from .login import authenticate_and_login, connect_and_login
from .metrics import swallowed
from .missions import DEFAULT_MISSION
from .pathing import PathClient
from .runloop import run_loop
from .supervisor import supervise_connection
from .unmet import UnmetIntents
from .worldknowledge import WorldKnowledge


@dataclass
class Agent:
    """One wired agent. Built by build_agent(); run() is the whole life of its connection."""
    cfg: object
    log: logging.Logger
    chat_relay: ChatRelay
    brain: Brain | None
    audit_logger: AuditLogger
    observer: AgentObserver | None
    http_server: ObserverServer | None
    lifecycle: Lifecycle
    duration: float | None = None      # None = forever
    start_delay: float = 0.0           # #539: one wait before the first connection attempt
    perception_dump: bool = False
    unmet: UnmetIntents | None = None  # #319: unmet-intent records, one per agent

    def build_session(self):
        """The supervisor's build_session(): a new, logged-in WoWSession."""
        sess = connect_and_login(self.cfg, self.log, self.chat_relay)
        sess.path_client = build_path_client(self.cfg)
        return sess

    def run_session(self, sess, stop: threading.Event) -> bool:
        """The supervisor's run_session(): the reflex threads and the think loop of one
        session attempt, started and stopped by run_loop(). True when the session dropped."""
        return run_loop(sess, self.cfg, self.duration, perception_dump=self.perception_dump,
                        brain=self.brain, audit_logger=self.audit_logger, observer=self.observer,
                        stop=stop, unmet=self.unmet)

    def run(self, stop: threading.Event | None = None, **supervisor_kwargs) -> None:
        """Supervise the connection until it ends, then close(). `stop` ends it (SIGTERM).
        Raises what supervise_connection() raises: AuthRejected after a permanent refusal,
        or the crash of a session. `supervisor_kwargs` (sleep, jitter, initial_backoff,
        max_backoff) are for tests."""
        stop = stop or threading.Event()
        supervisor_kwargs.setdefault("start_delay", self.start_delay)
        try:
            supervise_connection(build_session=self.build_session,
                                 run_session=lambda sess: self.run_session(sess, stop),
                                 log=self.log, stop=stop, lifecycle=self.lifecycle, **supervisor_kwargs)
        finally:
            self.close()

    def close(self) -> None:
        """Stop the chat relay and the observability server. Does not touch a session."""
        self.chat_relay.stop()
        if self.http_server is not None:
            self.http_server.shutdown()


def build_agent(cfg, log, duration: float | None = None, perception_dump: bool = False) -> Agent:
    """Wire one agent from its config. Starts the chat relay and, when AGENT_HTTP_PORT is
    set, the observability server; does not connect to the game (Agent.run() does)."""
    chat_relay = build_chat_relay(cfg, log)
    load_world_bundle(cfg, log)

    # #161: the one brain AGENT_BRAIN selects (llm by default; Jev is opt-in); its client
    # missing -> no think step, the agent idles.
    brain = Brain.from_config(cfg)
    warn_jev_unselected(cfg, log)
    if brain is not None:
        log.info("brain: %s", brain.describe())
    else:
        log.warning("AGENT_BRAIN=%s has no usable config (LLM_BASE_URL/LLM_MODEL or "
                    "JEV_BASE_URL/JEV_API_KEY) — "
                    "think step disabled, agent will idle", cfg.agent_brain)

    audit_logger = AuditLogger(cfg.agent_name, base_dir=cfg.audit_dir,
                               retention_days=cfg.audit_retention_days)
    log.info("audit log: %s (retention %d days)", audit_logger.agent_dir, audit_logger.retention_days)
    if brain is not None:
        brain.restore_jev_spend(cfg.audit_dir, cfg.agent_name)  # #213: budget survives restarts

    unmet = UnmetIntents(cfg.agent_name, getattr(cfg, "mission", DEFAULT_MISSION), audit_logger)  # #319
    observer, http_server = start_observer(cfg, audit_logger, log, brain=brain)
    if observer is not None:
        observer.unmet = unmet   # GET /unmet-intents
    return Agent(cfg=cfg, log=log, chat_relay=chat_relay, brain=brain, audit_logger=audit_logger,
                 observer=observer, http_server=http_server,
                 lifecycle=build_lifecycle(audit_logger, observer),
                 duration=duration, perception_dump=perception_dump, start_delay=cfg.start_delay,
                 unmet=unmet)


def guarded_lookup(wk, log):
    """spells.get_spell_info's lookup: wk.spell, but a reader that raises is counted, logged
    (throttled) and answers None, so the table entry stands. spells.py holds no broad except."""
    def lookup(spell_id: int):
        try:
            return wk.spell(spell_id)
        except Exception:  # noqa: BLE001 - never worse than the table
            swallowed("root.world_bundle", log)
            return None
    return lookup


def load_world_bundle(cfg, log) -> None:
    """ADR 0016 further slice 1: with AGENT_WORLD_BUNDLE set, open the bundle once, hand its
    lookup to spells.get_spell_info and log how it compares with SPELL_TABLE. Empty = nothing
    is touched."""
    path = getattr(cfg, "world_bundle", "")
    if not path:
        return
    lookup = guarded_lookup(WorldKnowledge(path), log)
    spells.set_world_knowledge(lookup)
    for line in spells.compare_with_table(lookup):
        log.info("%s", line)


def build_lifecycle(audit_logger, observer) -> Lifecycle:
    """#292: the supervisor's state machine (ADR 0015). Each transition is one line in the
    audit directory (AuditLogger.record_lifecycle) and GET /state shows the snapshot. The
    clock is the wall clock, so `since` and `at` are unix epoch seconds like the audit `ts`."""
    lifecycle = Lifecycle(clock=time.time, on_transition=lambda t: audit_logger.record_lifecycle(
        t.source.value, t.target.value, t.reason, ts=t.at))
    if observer is not None:
        observer.lifecycle = lifecycle
    return lifecycle


def list_characters(cfg, log) -> bool:
    """--list-chars: authenticate, log the character list, log out. False when the login
    failed (logged at CRITICAL)."""
    try:
        sess, _chars = authenticate_and_login(cfg, log)
    except RuntimeError as e:
        log.critical("%s", e)
        return False
    sess.logout()
    return True


def dry_run(cfg, log, duration: float) -> bool:
    """--dry-run: log in, stay `duration` seconds, log out. No supervisor, no brain, no audit
    log, no observer and so no lifecycle (ADR 0015, decision 5). False when the login failed
    (logged at CRITICAL)."""
    chat_relay = build_chat_relay(cfg, log)
    try:
        sess = connect_and_login(cfg, log, chat_relay)
    except RuntimeError as e:
        log.critical("%s", e)
        return False
    log.info("dry-run: sleeping %s s ...", duration)
    time.sleep(duration)
    log.info("perception: %d objects tracked (dry-run; recv thread %s, %d packets dropped)",
             len(sess.world_state.get_objects()),
             "alive" if sess.recv_thread_alive() else "DEAD",
             sess.dropped_packets)
    sess.logout()
    chat_relay.stop()
    log.info("dry-run complete.")
    return True


def warn_jev_unselected(cfg, log):
    """Warn when Jev is configured but AGENT_BRAIN selects another brain, whether
    or not that brain has a usable config."""
    if cfg.jev_enabled and cfg.agent_brain != "jev":
        log.warning("Jev is configured (JEV_BASE_URL/JEV_API_KEY) but not selected: "
                    "AGENT_BRAIN=%s, so Jev will never be called. Set AGENT_BRAIN=jev "
                    "to use it", cfg.agent_brain)


def start_observer(cfg, audit_logger, log, brain=None):
    """UM-50: start the read-only observability API if AGENT_HTTP_PORT is
    set. Returns (observer, server), both None when it's off. A port that
    can't be bound is logged and the agent plays on without it."""
    if not cfg.http_port:
        return None, None
    mission = getattr(cfg, "mission", DEFAULT_MISSION)
    if brain is not None:
        observer = AgentObserver(cfg.agent_name, goal=cfg.persona, model=brain.model,
                                 brain="jev" if brain.jev is not None else "llm", mission=mission)
    else:
        observer = AgentObserver(cfg.agent_name, goal=cfg.persona, model=cfg.llm_model, mission=mission)
    observer.fleet = getattr(brain, "fleet", None)  # #675: /metrics reads its health
    observer.control_token = cfg.control_token  # #178: "" keeps the API read-only
    if audit_logger is not None:
        audit_logger.on_record = observer.record_decision
    try:
        server = start_server(observer, cfg.http_bind, cfg.http_port)
    except OSError as e:
        log.error("observability API disabled: cannot bind %s:%d (%s)", cfg.http_bind, cfg.http_port, e)
        return None, None
    return observer, server


def build_path_client(cfg) -> PathClient | None:
    """#312: the one PathClient of a session, or None when AGENT_PATHFINDER_URL is empty (no sidecar,
    straight-line movement, no HTTP call)."""
    return PathClient(cfg.pathfinder_url) if cfg.pathfinder_url else None


def build_chat_relay(cfg, log) -> ChatRelay:
    """UM-47: one relay per agent process, shared by every (re)connected
    session. Inert unless AGENT_CHAT_RELAY_URL is set."""
    relay = ChatRelay(cfg.chat_relay_url, agent_name=cfg.agent_name,
                      token=cfg.chat_relay_token, timeout=cfg.chat_relay_timeout)
    if relay.enabled:
        relay.start()
        log.info("chat relay: %s", relay.url)
    return relay
