"""Atomic, versioned JSON for on-disk state (ADR 0018, slice 1).

The marker is an integer under the top-level key `schema`, next to the payload. An absent
`schema` is version 1, so every file written before the marker existed is a valid version 1.
Only a parsed integer above the reader's own version is NEWER; anything that does not parse
is UNREADABLE. What to do about each status is the caller's decision (ADR 0018, Decision).

Stdlib only, imports nothing from the package, keeps no state and does not log: every
function takes the path per call and the caller logs through its own logger.
"""

import contextlib
import json
import os
import threading
from typing import NamedTuple

SCHEMA_KEY = "schema"

MISSING = "MISSING"
UNREADABLE = "UNREADABLE"
OLDER = "OLDER"
OK = "OK"
NEWER = "NEWER"


class Loaded(NamedTuple):
    status: str       # MISSING, UNREADABLE, OLDER, OK or NEWER
    version: int      # the file's version; 0 when there is none (MISSING, UNREADABLE)
    data: dict        # the object without the schema key; empty unless OK or OLDER


def write_atomic(path: str, text: str) -> None:
    """Write `text` to `path` through a temporary file in the same directory: flush, fsync,
    then os.replace. On failure the old file is left as it was, the temporary file is removed
    and OSError is raised."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_versioned(path: str, current: int) -> Loaded:
    """Read `path` as a JSON object with an optional integer `schema`. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return Loaded(MISSING, 0, {})
    except (OSError, ValueError, RecursionError):   # UnicodeDecodeError is a ValueError
        return Loaded(UNREADABLE, 0, {})
    if not isinstance(doc, dict):
        return Loaded(UNREADABLE, 0, {})
    version = doc.pop(SCHEMA_KEY, 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return Loaded(UNREADABLE, 0, {})
    if version > current:
        return Loaded(NEWER, version, {})
    return Loaded(OK if version == current else OLDER, version, doc)


def write_versioned(path: str, current: int, body: dict) -> None:
    """write_atomic of `body` with `schema: current` at the top level. A body that already
    has a `schema` key is a bug in the caller: ValueError, nothing written."""
    if SCHEMA_KEY in body:
        raise ValueError(f"body must not carry the {SCHEMA_KEY!r} key")
    write_atomic(path, json.dumps({SCHEMA_KEY: current, **body}))


def set_aside(path: str) -> None:
    """Move an unreadable file to `<path>.corrupt`, replacing an older one. Best effort: OSError (and a path the OS rejects) is swallowed."""
    with contextlib.suppress(OSError, ValueError):   # ValueError: a path with a NUL byte
        os.replace(path, path + ".corrupt")
