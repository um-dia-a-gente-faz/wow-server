"""Per-section size caps for the perception snapshot (#367).

One table names every list-valued section and its cap, enforced by apply_caps
just before compose_snapshot returns. Under normal data every cap is a no-op;
the caps only bound what a crowd, a long quest log or a big mailbox can add to
the LLM prompt.
"""

from collections.abc import Mapping
from typing import Any, cast

from ..model import Snapshot

# A plain key names a top-level list; a dotted key `a.b` names list `b` inside
# dict snapshot["a"]. The nearby_* caps are overridden by compose_snapshot's
# `limit` argument.
SECTION_CAPS: dict[str, int] = {
    "nearby_units": 40, "nearby_players": 40, "nearby_objects": 40,
    "inventory": 20,        # slots 19-38
    "quest_log": 25,        # MAX_QUEST_LOG_SIZE
    "chat_inbox": 50,       # agent.state.CHAT_INBOX_MAXLEN
    "channels": 20,
    "mailbox.mails": 50,
    "group.members": 39,    # raid of 40 minus self
}
# Sections that drop from the front (keep the newest); every other section drops from the end.
KEEP_NEWEST = frozenset({"chat_inbox"})


def _check_caps(caps: Mapping[str, int]) -> None:
    for key, cap in caps.items():
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ValueError(f"cap for {key!r} must be a positive int, got {cap!r}")


def apply_caps(snapshot: Snapshot, caps: Mapping[str, int] = SECTION_CAPS) -> Snapshot:
    """Return a copy of `snapshot` with each capped list cut to its cap. Nested
    parents are copied, never mutated, because mailbox and group are shared
    with WorldState and the session. `truncated` maps each cut key to the
    number of items dropped, and is absent when nothing was cut."""
    _check_caps(caps)
    out: dict[str, Any] = dict(snapshot)
    truncated: dict[str, int] = {}
    for key, cap in caps.items():
        parent_key, _, leaf = key.rpartition(".")
        owner: dict[str, Any] | None = out
        if parent_key:
            owner = out.get(parent_key)
        if not isinstance(owner, dict):
            continue
        section = owner.get(leaf)
        if not isinstance(section, list) or len(section) <= cap:
            continue
        kept = section[-cap:] if key in KEEP_NEWEST else section[:cap]
        truncated[key] = len(section) - cap
        if parent_key:
            out[parent_key] = {**owner, leaf: kept}
        else:
            out[key] = kept
    if truncated:
        out["truncated"] = truncated
    return cast(Snapshot, out)
