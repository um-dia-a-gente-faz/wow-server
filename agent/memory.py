#!/usr/bin/env python3
"""Durable agent memory (#276): what the agent learned, kept across restarts.

A small sqlite3 store, one file per agent. This module takes the path it is given;
the consumers that wire it in (and the volume) come in later PRs. Memory is advisory:
validate a recalled value against live perception before acting on it.

Writes go through one writer thread and a queue, so `remember` never waits on disk.
`recall` and `recent` read through a second connection (in-memory mode: the same connection,
behind the same lock). Call `flush()` first to see your own writes from the same process. A missing directory is created; a corrupt or
too-new file degrades to an empty in-memory store. No public method raises.
See docs/adr/0011-agent-memory-store.md.
"""

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable

log = logging.getLogger(__name__)

# Forward-only: append a migration, never edit one that has shipped.
MIGRATIONS = (
    """CREATE TABLE memory (
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        expires_at REAL,
        PRIMARY KEY (kind, key))""",
)
SCHEMA_VERSION = len(MIGRATIONS)
_CLOSE_TIMEOUT_S = 5.0


class Memory:
    def __init__(self, path: str | None, now: Callable[[], float] = time.time):
        self._now = now
        self._closed = False
        self._warned = False
        self._ops: queue.SimpleQueue = queue.SimpleQueue()
        self._io = threading.Lock()         # the writer holds it per operation
        self._writer, self._reader = _open(path, self._warn)
        # Shared-cache in-memory mode has one connection; readers and the writer must take turns on it.
        # Two connections there hit "database table is locked" and drop writes.
        self._read_lock = self._io if self._reader is self._writer else threading.Lock()
        self._thread = threading.Thread(target=self._run, name="agent-memory-writer", daemon=True)
        self._thread.start()

    def remember(self, kind: str, key: str, value: object, ttl: float | None = None) -> None:
        """Queue a write and return. `ttl` is in seconds; None never expires."""
        if self._closed:
            return
        try:
            text = json.dumps(value)
        except (TypeError, ValueError) as e:
            log.warning("agent memory: %s/%s is not JSON, dropped (%s)", kind, key, e)
            return
        expires = None if ttl is None else self._now() + ttl
        self._ops.put(("INSERT OR REPLACE INTO memory VALUES (?, ?, ?, ?)", (kind, key, text, expires)))

    def recall(self, kind: str, key: str) -> object | None:
        """The stored value, or None when missing, expired or unreadable."""
        rows = self._read("SELECT value, expires_at FROM memory WHERE kind = ? AND key = ?", (kind, key))
        if not rows:
            return None
        text, expires = rows[0]
        if expires is not None and expires <= self._now():
            return None
        return _decode(text)

    def recent(self, kind: str, n: int) -> list[tuple[str, object]]:
        """Up to n unexpired (key, value) pairs of `kind`, newest write first."""
        if n <= 0:
            return []
        rows = self._read(
            "SELECT key, value FROM memory WHERE kind = ? AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY rowid DESC LIMIT ?",
            (kind, self._now(), n))
        return [(key, _decode(text)) for key, text in rows]

    def flush(self) -> None:
        """Block until every write queued before this call has reached the file."""
        if self._closed:
            return
        done = threading.Event()
        self._ops.put(done)
        while not done.wait(0.1):
            if not self._thread.is_alive():
                return

    def close(self) -> None:
        """Flush what is queued, stop the writer thread and close both connections."""
        if self._closed:
            return
        self._closed = True
        self._ops.put(None)
        self._thread.join(_CLOSE_TIMEOUT_S)
        with self._read_lock:
            self._reader.close()

    def _run(self) -> None:
        try:
            while True:
                op = self._ops.get()
                if op is None:
                    break
                if isinstance(op, threading.Event):
                    op.set()
                    continue
                sql, params = op
                with self._io:
                    try:
                        self._writer.execute(sql, params)
                    except sqlite3.Error as e:
                        self._warn("write failed", e)
        finally:
            with self._io:   # in-memory mode shares this connection with the readers
                self._writer.close()

    def _read(self, sql: str, params: tuple) -> list:
        if self._closed:
            return []
        with self._read_lock:
            if self._closed:   # close() began while this call waited for the lock: the connection may be closed
                return []
            try:
                return self._reader.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                self._warn("read failed", e)
                return []

    def _warn(self, what: str, exc: Exception) -> None:
        if not self._warned:   # once per store: a broken file would otherwise log on every call
            self._warned = True
            log.warning("agent memory: %s (%s); continuing without it on disk", what, exc)


def _open(path: str | None, warn: Callable[[str, Exception], None]) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Return (writer, reader) connections to the store, migrated to SCHEMA_VERSION.

    The reader is the writer itself for the in-memory store."""
    if path is not None:
        writer = None
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            writer = _connect(path, uri=False)
            _migrate(writer)
            return writer, _connect(path, uri=False)
        except (sqlite3.Error, OSError) as e:
            if writer is not None:
                writer.close()
            warn(f"{path} unusable", e)
    # A private in-memory database. One connection: a second one to a shared cache would lock out the writer.
    conn = _connect(f"file:agent-memory-{uuid.uuid4().hex}?mode=memory&cache=shared", uri=True)
    _migrate(conn)
    return conn, conn


def _connect(target: str, uri: bool) -> sqlite3.Connection:
    # The writer thread closes the writer; the reader is closed under its lock by close().
    return sqlite3.connect(target, uri=uri, check_same_thread=False, isolation_level=None, timeout=5.0)


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"schema version {version} is newer than {SCHEMA_VERSION}")
    for n in range(version, SCHEMA_VERSION):
        conn.executescript(f"BEGIN;\n{MIGRATIONS[n]};\nPRAGMA user_version = {n + 1};\nCOMMIT;")


def _decode(text: str) -> object | None:
    try:
        return json.loads(text)
    except ValueError:
        return None
