"""Connection lifecycle state machine (#584): the states the reconnect supervisor
moves through, as a pure module. agent/supervisor.py drives it (#292).

Player state (alive, dead, ghost) is not here: it is game state (agent/death.py).
Stdlib only, and no logging: the owner of a Lifecycle decides what to do on a change.
"""
import enum
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


class State(enum.Enum):
    CONNECTING = "connecting"   # build_session() running: auth, realm, world login
    ONLINE = "online"           # in world, run_session() running
    BACKOFF = "backoff"         # waiting before the next connect attempt
    STOPPING = "stopping"       # stop requested, the run ended or the supervisor is unwinding; logging out if a session exists
    STOPPED = "stopped"         # terminal: the supervisor returned or was unwound; the reason says which
    FAILED = "failed"           # terminal, permanent refusal (AuthRejected with a permanent code)


TRANSITIONS: dict[State, frozenset[State]] = {
    State.CONNECTING: frozenset({State.ONLINE, State.BACKOFF, State.FAILED, State.STOPPING}),
    State.ONLINE: frozenset({State.BACKOFF, State.STOPPING}),
    State.BACKOFF: frozenset({State.CONNECTING, State.STOPPING}),
    State.STOPPING: frozenset({State.STOPPED}),
    State.STOPPED: frozenset(),
    State.FAILED: frozenset(),
}
TERMINAL = frozenset({State.STOPPED, State.FAILED})

# The reasons the supervisor passes to to(). The machine accepts any text; this is the list
# the supervisor, the audit log and ADR 0015 agree on (a test compares it with the ADR).
REASONS = frozenset({"connected", "connect_failed", "auth_rejected", "auth_rejected_permanent",
                     "disconnected", "backoff_elapsed", "stop", "run_ended", "crashed"})


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    source: State
    target: State
    reason: str   # free text; the supervisor passes one of REASONS
    at: float     # clock() at the transition


class Lifecycle:
    """One connection's lifecycle. Thread-safe: every method takes one lock. The
    callback runs after the lock is released, so it may read the machine.

    One writer: only the thread that runs the supervisor calls to(), and the callback
    never calls to(). Then callbacks run in transition order. Other threads read (state,
    snapshot, history). The lock is not re-entrant: never touch a Lifecycle from a signal
    handler, which runs on the main thread and would deadlock inside to()."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 on_transition: Callable[[Transition], None] | None = None,
                 history: int = 32) -> None:
        self._clock = clock
        self._on_transition = on_transition
        self._lock = threading.Lock()
        self._state = State.CONNECTING
        self._since = clock()
        self._reason = ""
        self._history: deque[Transition] = deque(maxlen=history)

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def to(self, target: State, reason: str = "") -> Transition:
        with self._lock:
            if target not in TRANSITIONS[self._state]:
                allowed = ", ".join(sorted(s.value for s in TRANSITIONS[self._state])) or "none"
                raise InvalidTransition(
                    f"{self._state.value} -> {target.value} is not allowed (allowed: {allowed})")
            at = self._clock()
            t = Transition(self._state, target, reason, at)
            self._state = target
            self._since = at
            self._reason = reason
            self._history.append(t)
        if self._on_transition is not None:
            self._on_transition(t)
        return t

    def history(self) -> tuple[Transition, ...]:
        with self._lock:
            return tuple(self._history)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state.value,
                "since": self._since,
                "reason": self._reason,
                "transitions": [{"from": t.source.value, "to": t.target.value, "reason": t.reason, "at": t.at}
                                for t in self._history],
            }
