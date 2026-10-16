"""The feed event shape (UM-76) and the events an agent writes itself (ADR 0020, #390).

Every source of activity.py builds its events with `event()`. `agent_events` turns the `events`
of one audit line into events of that shape. Kept apart from activity.py, which is at its size
budget (agent/tests/test_size_budget.py).
"""
MAX_TEXT = 300
# ADR 0020 (#390): the activity kinds an agent may put in its audit line's `events`, with the detail
# keys each must carry as ints. Mirrors agent/audit_schema.py ACTIVITY_KINDS; any other kind is skipped.
AGENT_EVENT_KINDS = {"level": ("to",)}


def _clip(text, limit=MAX_TEXT):
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def event(character, t, kind, text, source, uid, detail=None, inferred=False):
    return {"character": character, "t": float(t), "kind": kind, "text": _clip(text),
            "detail": detail or {}, "source": source, "inferred": bool(inferred), "uid": uid}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def agent_events(rec, agent):
    """The feed events of one audit line's `events` (ADR 0020, #390). Never raises: an element that
    is not a known kind with its typed detail keys is skipped (see skipped_agent_events)."""
    raw = rec.get("events")
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        kind, detail, t = e.get("kind"), e.get("detail"), e.get("t")
        if not isinstance(kind, str) or kind not in AGENT_EVENT_KINDS or not isinstance(detail, dict):
            continue
        if not _is_number(t) or not all(_is_int(detail.get(k)) for k in AGENT_EVENT_KINDS[kind]):
            continue
        # The only kind so far is `level`; its text is the one wowmap writes for a level-up.
        out.append(event(agent, t, kind, f"Reached level {detail['to']}", "agent",
                         f"agent:{agent}:{kind}:{t:.3f}:{detail['to']}", detail))
    return out


def skipped_agent_events(rec, kept):
    """How many elements of rec["events"] agent_events() refused (`kept` is what it returned)."""
    raw = rec.get("events")
    if raw is None:
        return 0
    return 1 if not isinstance(raw, list) else len(raw) - len(kept)


def fact_key(e):
    """What a feed event states as a fact, for de-duplication (ADR 0020). `level` only, for now:
    (character, kind, to). None for every other event."""
    if e.get("kind") != "level":
        return None
    return (e["character"].lower(), "level", (e.get("detail") or {}).get("to"))


# The store's two rules (ADR 0020), on its connection; level only, as fact_key is.
def agent_fact_stored(conn, key):
    """True when an agent row already holds the fact `key`: a db event of it is then not stored."""
    return conn.execute(
        "SELECT 1 FROM events WHERE source = 'agent' AND kind = 'level' AND character = ?"
        " AND json_extract(detail_json, '$.to') = ? LIMIT 1", (key[0], key[2])).fetchone() is not None


def drop_db_fact(conn, key):
    """Delete the db rows of the fact `key`: an agent row for it has just been stored."""
    conn.execute(
        "DELETE FROM events WHERE source = 'db' AND kind = 'level' AND character = ?"
        " AND json_extract(detail_json, '$.to') = ?", (key[0], key[2]))
