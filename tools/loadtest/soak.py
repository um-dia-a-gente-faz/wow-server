"""Soak harness (#299): N real agent sessions (auth, WoWSession, recv thread, think loop,
reconnect supervisor) against an in-process tools/world-mock, with CPU, RSS, thread,
tick-period and reconnect numbers recorded into one JSON report.

Everything runs in this one process, so RSS/CPU/threads include the mock and the
harness; per-agent RSS is (peak RSS - RSS before the run) / N. Only ever talks to a
mock it started itself, on a loopback address (see check_host)."""

import collections
import ipaddress
import itertools
import json
import logging
import pathlib
import statistics
import sys
import threading
import time
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "tools" / "world-mock"):
    sys.path.insert(0, str(p))
import server  # noqa: E402  (tools/world-mock)

from agent.__main__ import _connect_and_login, _run_think_loop, _supervise_connection  # noqa: E402

PASSWORD = "soakpass"   # made-up, only ever registered in the throwaway mock
BACKOFF_S = 0.2         # reconnect backoff: short, the soak is about recovery, not politeness


def check_host(host: str, allow_non_loopback: bool = False) -> None:
    """Refuse anything but loopback: the soak must never be pointed at the live realm."""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not allow_non_loopback:
        raise SystemExit(f"refusing non-loopback host {host!r}: the soak only targets its own world-mock")


def read_rss_kb() -> int:
    """Resident set size from /proc (Linux), in kB."""
    with open("/proc/self/status") as f:
        return next(int(line.split()[1]) for line in f if line.startswith("VmRSS:"))


def container_items(*objs) -> int:
    """Total len() of every list/dict/set/deque held directly by these objects: a crude
    but uniform "does anything grow without bound" probe, no per-collection knowledge."""
    kinds = (list, dict, set, collections.deque)
    return sum(len(v) for o in objs for v in getattr(o, "__dict__", {}).values() if isinstance(v, kinds))


def pct(values, q):
    vals = sorted(values)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


class TickRecorder(logging.Handler):
    """The think loop logs "perception: ..." once per cycle at INFO; the gap between two
    of them on one agent thread is the real tick period (no change to agent/ needed)."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.last, self.periods = {}, []

    def emit(self, record):
        if str(record.msg).startswith("perception:"):
            now, prev = time.monotonic(), self.last.get(record.threadName)
            self.last[record.threadName] = now
            if prev is not None:
                self.periods.append(now - prev)    # list.append is atomic; no lock needed

    def new_session(self):
        self.last.pop(threading.current_thread().name, None)


class _Done(Exception):
    pass


class Agent:
    """One soak agent: the real supervisor over the real login and think loop."""

    def __init__(self, idx, mock, ticks, deadline, think_interval):
        self.name = f"SOAK{idx}"
        self.ticks, self.deadline = ticks, deadline
        self.cfg = SimpleNamespace(
            wow_host=mock.host, wow_auth_port=mock.auth_port, account=self.name, password=PASSWORD,
            verbose_packets=False, dump_packets_dir="", character=None, char_guid=0, channels="none",
            think_interval=think_interval, persona="")
        self.session = None
        self.builds = self.dropped = 0
        self.recoveries = []       # seconds from "went down" to "online again"
        self.down_since = None
        self.log = logging.getLogger("soak." + self.name)

    def _build(self):
        self.builds += 1
        self.ticks.new_session()
        try:
            self.session = _connect_and_login(self.cfg, self.log)
        except Exception:
            self.down_since = self.down_since or time.monotonic()   # a failed login is still "down"
            raise
        if self.down_since is not None:
            self.recoveries.append(time.monotonic() - self.down_since)
            self.down_since = None
        return self.session

    def _run(self, sess):
        down = _run_think_loop(sess, self.cfg, self.deadline - time.monotonic(), time.monotonic())
        self.dropped += sess.dropped_packets
        if down:
            self.down_since = time.monotonic()
        return down

    def _sleep(self, backoff):
        if time.monotonic() >= self.deadline:
            raise _Done
        time.sleep(backoff)

    def run(self):
        try:
            _supervise_connection(self._build, self._run, self.log, sleep=self._sleep,
                                  initial_backoff=BACKOFF_S, max_backoff=BACKOFF_S)
        except _Done:
            pass


def sample(agents, t0, cpu0):
    now, cpu = time.monotonic(), time.process_time()
    live = [a.session for a in agents if a.session is not None]
    return {"t": round(now - t0, 1), "rss_kb": read_rss_kb(), "threads": threading.active_count(),
            "cpu_pct": round(100 * (cpu - cpu0) / max(now - t0, 1e-6), 1),
            "container_items": sum(container_items(s, getattr(s, "world_state", None)) for s in live)}


def run_soak(agents: int, duration: float, faults: str = "", think_interval: float = 0.1,
             host: str = "127.0.0.1", sample_every: float = 1.0, allow_non_loopback: bool = False) -> dict:
    check_host(host, allow_non_loopback)
    plans = server.FaultPlan.parse(faults) if faults else [server.FaultPlan()]
    threads0, rss0 = threading.active_count(), read_rss_kb()
    mock = server.WorldMock(host=host, faults=itertools.cycle(plans))
    mock.accounts = {f"SOAK{i}": PASSWORD for i in range(agents)}
    ticks = TickRecorder()
    agent_log = logging.getLogger("agent")
    saved = (agent_log.level, agent_log.propagate, agent_log.handlers[:])
    agent_log.handlers, agent_log.propagate = [ticks], False    # quiet, and the recorder sees every record
    agent_log.setLevel(logging.INFO)
    t0 = time.monotonic()
    cpu0 = time.process_time()
    deadline = t0 + duration
    pool = [Agent(i, mock, ticks, deadline, think_interval) for i in range(agents)]
    workers = [threading.Thread(target=a.run, name=a.name, daemon=True) for a in pool]
    for w in workers:
        w.start()
    samples = []
    try:
        while any(w.is_alive() for w in workers) and time.monotonic() < deadline + 30:
            time.sleep(sample_every)
            samples.append(sample(pool, t0, cpu0))
    finally:
        for w in workers:
            w.join(timeout=10)
        mock.close()
        agent_log.handlers, agent_log.propagate = saved[2], saved[1]
        agent_log.setLevel(saved[0])
    time.sleep(0.5)    # let the mock's handler threads see the closed sockets and exit
    return report(pool, mock, ticks, samples, think_interval, duration, threads0, rss0, faults)


def report(pool, mock, ticks, samples, interval, duration, threads0, rss0, faults) -> dict:
    n = len(pool)
    rec = [r for a in pool for r in a.recoveries]
    peak = max((s["rss_kb"] for s in samples), default=rss0)
    threads_peak = max((s["threads"] for s in samples), default=threads0)
    items = [s["container_items"] for s in samples]
    ms = lambda v: None if v is None else round(v * 1000, 1)    # noqa: E731
    return {
        "agents": n, "duration_s": duration, "think_interval_ms": interval * 1000, "faults": faults,
        "rss_before_kb": rss0, "rss_peak_kb": peak, "rss_end_kb": samples[-1]["rss_kb"] if samples else rss0,
        "rss_per_agent_kb": round((peak - rss0) / n),
        "threads_before": threads0, "threads_peak": threads_peak, "threads_end": threading.active_count(),
        "threads_per_agent": round((threads_peak - threads0) / n, 1),
        "cpu_pct_mean": round(statistics.fmean(s["cpu_pct"] for s in samples), 1) if samples else None,
        "tick_period_ms": {"count": len(ticks.periods), "p50": ms(pct(ticks.periods, .5)),
                           "p99": ms(pct(ticks.periods, .99)), "max": ms(max(ticks.periods, default=None))},
        "reconnects": sum(a.builds - 1 for a in pool), "auth_connections": mock.auth_connections,
        "world_connections": mock.world_connections,
        "recovery_s": {"count": len(rec), "p50": round(pct(rec, .5), 2) if rec else None,
                       "max": round(max(rec), 2) if rec else None},
        "dropped_packets": sum(a.dropped for a in pool),
        "container_items": {"first": items[0] if items else None, "last": items[-1] if items else None,
                            "max": max(items, default=None)},
        "samples": samples,
    }


def compare(report_: dict, baseline: dict, tolerance: float = 0.2) -> list[str]:
    """Regressions of per-agent cost against a baseline report; empty list = fine."""
    out = []
    for key in ("rss_per_agent_kb", "threads_per_agent"):
        base, now = baseline.get(key), report_.get(key)
        if base and now is not None and now > base * (1 + tolerance):
            out.append(f"{key}: {now} vs baseline {base} (+{(now / base - 1):.0%}, limit {tolerance:.0%})")
    return out


def load(path) -> dict:
    return json.loads(pathlib.Path(path).read_text())
