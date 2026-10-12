"""Deploy log reader for the exporter (#377). Stdlib only, so it is testable without pymysql.

Reads the lines `scripts/deploy.sh` appends to `deploy.log`: `<date -Is> deployed <sha7>, stacks: <list>`.
"""
import os
import re
from datetime import datetime

TAIL_BYTES = 65536  # the file grows forever: only its end is read
LINE = re.compile(r"(\S+) deployed ([0-9a-f]{7,40}), stacks: ([a-z-]+(?: [a-z-]+)*)")


def parse_deploys(text: str, limit: int = 20) -> list[tuple[float, str, str]]:
    """(epoch seconds, sha, stacks) of the newest `limit` deploy lines, oldest first.

    `stacks` has its spaces replaced by `+` (`game monitoring` -> `game+monitoring`). A line that
    does not match (DEFERRED, garbage, a torn line, a time without an offset) is skipped.
    """
    found = []
    for line in text.split("\n"):
        m = LINE.fullmatch(line)
        if not m:
            continue
        try:
            when = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        if when.tzinfo is None:
            continue
        found.append((when.timestamp(), m.group(2), m.group(3).replace(" ", "+")))
    return found[max(0, len(found) - limit):]


def read_deploys(path, limit: int = 20) -> list[tuple[float, str, str]]:
    """parse_deploys over the last TAIL_BYTES of the file. A missing or unreadable file gives []."""
    try:
        with open(path, "rb") as f:
            start = max(0, f.seek(0, os.SEEK_END) - TAIL_BYTES)
            f.seek(start)
            data = f.read()
    except (OSError, ValueError):
        return []
    text = data.decode("utf-8", errors="replace")
    if start:  # the window began mid-line: drop that partial first line
        text = text.partition("\n")[2]
    return parse_deploys(text, limit)
