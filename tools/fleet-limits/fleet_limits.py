#!/usr/bin/env python3
"""The writer of the fleet limits file (ADR 0017 further slice 2, #745, part of #372).

One host process on `wow-agents`. Every FLEET_LIMITS_PERIOD_S it sums the last hour of every
agent's audit log, blocks a brain while its fleet cap is spent, merges the owner's stop file and
rewrites FLEET_LIMITS_FILE atomically. The agents read that file (`agent/fleetlimits.py`) and fail
open, so this process never turns its own trouble into a block: an unreadable input counts as no
usage, a failed write leaves the old file, whose computed blocks expire within BLOCK_S by themselves.

Stdlib only. It imports nothing from agent/ or from other tools: the audit lines are parsed here
and the file format is the `version` 1 one documented at the top of agent/fleetlimits.py. A test pins
the two together. Usage: `fleet_limits.py [--once]`; the settings are environment variables, see README.md.
"""

import argparse
import contextlib
import json
import logging
import math
import os
import signal
import stat
import sys
import tempfile
import threading
import time

log = logging.getLogger("fleet_limits")

SCHEMA_VERSION = 1       # the reader's SCHEMA_VERSION; any other value reads as no limits at all
WINDOW_S = 3600          # the rolling hour
BLOCK_S = 30             # a computed block lasts two periods; only the owner's stop may be longer
REASON_MAX_CHARS = 200   # the reader cuts at 200; cut here too so what is written is what is read
MAX_BYTES = 65536        # the reader ignores a larger file, so the owner file is bounded the same way
FILE_MODE = 0o644        # the agents run as another user: readable by all, writable by this one
BRAINS = ("jev", "llm")


def _num(v):
    """A finite int or float as a float, else None (bool, str, NaN, inf and None are not numbers)."""
    if type(v) not in (int, float):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _tokens(*values):
    """The sum of the token fields: a missing one is 0, a present one that is not a non-negative
    number voids the whole record's share (never an error)."""
    total = 0.0
    for v in values:
        if v is None:
            continue
        n = _num(v)
        if n is None or n < 0:
            return 0
        total += n
    return int(total)


def hour_sums(records, now):
    """(jev_tokens, llm_tokens) over records with now - 3600 < ts <= now. Jev tokens are
    usage.input_tokens + usage.output_tokens (the sum Brain.restore_jev_spend takes; `usage` holds
    Jev usage only); LLM tokens are prompt_tokens + completion_tokens of records whose brain is "llm"."""
    jev = llm = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        ts = _num(rec.get("ts"))
        if ts is None or not now - WINDOW_S < ts <= now:
            continue
        usage = rec.get("usage")
        if isinstance(usage, dict):
            jev += _tokens(usage.get("input_tokens"), usage.get("output_tokens"))
        if rec.get("brain") == "llm":
            llm += _tokens(rec.get("prompt_tokens"), rec.get("completion_tokens"))
    return jev, llm


def _ignore(key, message, exc_info=False):
    pass


def _owner_blocks(owner_stop, now, warn):
    """The valid, unexpired entries of the owner's stop. An entry for another brain or without a
    finite numeric until_ts is dropped with a warning. An expired entry is not a block, so it must
    not shadow a computed one."""
    if owner_stop is None:
        return {}
    if not isinstance(owner_stop, dict):
        warn("owner-shape", "owner stop: top level is not an object, ignored")
        return {}
    out = {}
    for brain, entry in owner_stop.items():
        until = _num(entry.get("until_ts")) if isinstance(entry, dict) else None
        if brain not in BRAINS or until is None:
            warn(f"owner-entry-{str(brain)[:40]}",
                 f"owner stop entry {str(brain)[:40]!r} dropped: want jev or llm with a finite numeric until_ts")
            continue
        if until <= now:
            continue
        reason = entry.get("reason")
        text = " ".join(reason.split()) if isinstance(reason, str) else ""
        out[brain] = {"until_ts": until, "reason": (text or "stopped by the owner")[:REASON_MAX_CHARS]}
    return out


def build(sums, now, jev_cap, llm_cap, owner_stop=None, warn=_ignore):
    """The file document for already-summed usage. A cap of 0 is off; at or above it, blocked."""
    blocks = {}
    for brain, spent, cap in (("jev", sums[0], jev_cap), ("llm", sums[1], llm_cap)):
        if cap > 0 and spent >= cap:
            reason = f"fleet {brain} budget spent: {spent} of {cap} tokens in the last hour"
            blocks[brain] = {"until_ts": now + BLOCK_S, "reason": reason[:REASON_MAX_CHARS]}
    blocks.update(_owner_blocks(owner_stop, now, warn))   # the owner wins, with their own until_ts
    return {"version": SCHEMA_VERSION, "written_ts": now, "blocks": blocks}


def compute(records, now, jev_cap, llm_cap, owner_stop, warn=_ignore):
    """Pure: the limits file document for `records` (dicts from every agent's audit log) at `now`.
    `owner_stop` is the parsed owner file or None."""
    return build(hour_sums(records, now), now, jev_cap, llm_cap, owner_stop, warn)


# ── shell ────────────────────────────────────────────────────────────

class Warner:
    """Warn once per distinct problem, not once per cycle: a problem logged last cycle and present
    again is quiet; one that cleared and comes back is logged again."""

    def __init__(self):
        self._seen: set[str] = set()
        self._now: set[str] = set()

    def new_cycle(self):
        self._seen, self._now = self._now, set()

    def __call__(self, key, message, exc_info=False):
        self._now.add(key)
        if key not in self._seen:
            log.warning("%s", message, exc_info=exc_info)


def _read_file(path):
    with open(path, "rb") as f:
        for line in f:
            try:
                rec = json.loads(line.decode("utf-8", "replace"))
            except (ValueError, RecursionError):   # a truncated last line, a blank one, garbage
                continue
            if isinstance(rec, dict):
                yield rec


def read_audit(base, now, warn):
    """Records of `<base>/<agent>/<day>.jsonl[.N]` files modified within the window. The layout
    rule is agent/metrics.py::find_audit_files, mirrored: one level of agent directories, so the
    `<agent>/lifecycle/` subdirectory is never read. A file that vanishes between listing and
    opening (rotation) is skipped quietly; any other trouble is a warning and that input counts
    as no usage."""
    cutoff = now - WINDOW_S
    try:
        agents = sorted(e.path for e in os.scandir(base) if e.is_dir())
    except OSError as e:
        warn("audit-dir", f"audit directory unreadable ({type(e).__name__}), counted as no usage")
        return
    for agent_dir in agents:
        try:
            names = sorted(os.listdir(agent_dir))
        except OSError as e:
            warn(f"list:{agent_dir}", f"audit directory {agent_dir[-80:]!r} unreadable ({type(e).__name__})")
            continue
        for name in names:
            if name.startswith(".") or not (name.endswith(".jsonl") or ".jsonl." in name):
                continue
            path = os.path.join(agent_dir, name)
            try:
                st = os.stat(path)
                # A file last written before the window holds nothing in it (Brain.restore_jev_spend
                # skips it the same way).
                # ponytail: the files of the last hour are re-read whole every period (25 agents:
                # a few MB). Ceiling: tens of MB a cycle. Upgrade: remember an offset per file
                # (keyed by inode) and a running per-agent window.
                if stat.S_ISREG(st.st_mode) and st.st_mtime > cutoff:
                    yield from _read_file(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                warn(f"read:{path}", f"audit file {path[-80:]!r} unreadable ({type(e).__name__}), counted as no usage")


def read_owner(path, previous, warn):
    """The parsed owner stop file. No file: None (the owner removed it). A file that cannot be read
    or is not a JSON object: a warning and `previous`, so a half-saved edit does not lift the stop."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as e:
        warn("owner-read", f"owner stop file unreadable ({type(e).__name__}), keeping the previous one")
        return previous
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("too big")
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not an object")
    except (ValueError, RecursionError) as e:
        warn("owner-invalid", f"owner stop file invalid ({e}), keeping the previous one")
        return previous
    return doc


def _write_atomic(path, text):
    """Temporary file in the same directory, fsync, os.replace: a reader sees the old file or the
    new one, never half of one. The temporary is removed on any failure and the target untouched.
    Mode FILE_MODE whatever the umask. No directory fsync (ADR 0018)."""
    directory, name = os.path.split(os.fspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fchmod(f.fileno(), FILE_MODE)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Settings:
    def __init__(self, env):
        def get(name, default, parse, ok):
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = parse(raw)
            except ValueError:
                raise ValueError(f"{name} is not a number") from None
            if not ok(value):
                raise ValueError(f"{name} is out of range")
            return value

        for name in ("FLEET_AUDIT_DIR", "FLEET_LIMITS_FILE"):
            if not env.get(name):
                raise ValueError(f"{name} is required")
        self.audit_dir = env["FLEET_AUDIT_DIR"]
        self.limits_file = env["FLEET_LIMITS_FILE"]
        self.owner_file = env.get("FLEET_OWNER_STOP_FILE") or os.path.join(
            os.path.dirname(self.limits_file), "owner-stop.json")
        self.jev_cap = get("FLEET_JEV_TOKENS_PER_HOUR", 0, int, lambda v: v >= 0)
        self.llm_cap = get("FLEET_LLM_TOKENS_PER_HOUR", 0, int, lambda v: v >= 0)
        self.period_s = get("FLEET_LIMITS_PERIOD_S", 15.0, float, lambda v: math.isfinite(v) and v > 0)


class Writer:
    def __init__(self, settings):
        self.s = settings
        self.warn = Warner()
        self.owner = None   # the last valid owner stop, kept while the file is unreadable

    def cycle(self, now):
        """One read-compute-write pass. Returns (document, (jev, llm) sums, whether the file was written)."""
        self.warn.new_cycle()
        self.owner = read_owner(self.s.owner_file, self.owner, self.warn)
        sums = hour_sums(read_audit(self.s.audit_dir, now, self.warn), now)
        doc = build(sums, now, self.s.jev_cap, self.s.llm_cap, self.owner, self.warn)
        try:
            _write_atomic(self.s.limits_file, json.dumps(doc, sort_keys=True) + "\n")
        except OSError as e:
            self.warn("write", f"cannot write the limits file ({type(e).__name__}: {e.strerror}), "
                               f"retrying next period; the old file stays")
            return doc, sums, False
        return doc, sums, True


def _summary(s, sums, doc):
    caps = (s.jev_cap or "off", s.llm_cap or "off")
    return (f"last hour: jev {sums[0]} of {caps[0]} tokens, llm {sums[1]} of {caps[1]} tokens; "
            f"blocked: {', '.join(sorted(doc['blocks'])) or 'none'}")


def main(argv=None, env=None, clock=time.time, stop=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run one cycle, log the hour's sums and exit")
    once = parser.parse_args(argv).once
    try:
        settings = Settings(os.environ if env is None else env)
    except ValueError as e:
        print(f"fleet_limits: {e}", file=sys.stderr)
        return 2
    writer = Writer(settings)
    if once:
        doc, sums, ok = writer.cycle(clock())
        log.info("%s", _summary(settings, sums, doc))
        return 0 if ok else 1
    own_signal = stop is None
    stop = stop or threading.Event()
    if own_signal:
        previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    last = None
    try:
        while not stop.is_set():
            try:
                doc, sums, _ = writer.cycle(clock())
            except Exception as e:   # noqa: BLE001 - one bad cycle must not end the process
                writer.warn(f"cycle:{type(e).__name__}", f"cycle failed ({type(e).__name__}: {str(e)[:200]})",
                            exc_info=True)
            else:
                blocked = tuple(sorted(doc["blocks"]))
                if blocked != last:   # at start and whenever the set of blocked brains changes
                    log.info("%s", _summary(settings, sums, doc))
                    last = blocked
            stop.wait(settings.period_s)
    except KeyboardInterrupt:
        pass
    finally:
        if own_signal:
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
