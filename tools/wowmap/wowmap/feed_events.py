"""The feed event shape (UM-76) and the events an agent writes itself (ADR 0020, #390).

Every source of activity.py builds its events with `event()`. `agent_events` turns the `events`
of one audit line into events of that shape, and `audit_event` the decision of the line itself.
Kept apart from activity.py, which is at its size budget (agent/tests/test_size_budget.py).
"""
import json

MAX_TEXT = 300
# Decisions that mean "do nothing this cycle" -- not worth a feed row.
SKIPPED_ACTIONS = frozenset({"idle"})
ACTION_KINDS = {
    "auto_attack": "combat", "stop_attack": "combat", "cast_spell": "combat",
    "set_target": "combat", "assist": "combat",
    "loot": "loot", "sell_item": "sell", "buy_item": "buy",
    "open_trade": "trade", "accept_trade_request": "trade", "offer_item": "trade",
    "offer_gold": "trade", "accept_trade": "trade", "cancel_trade": "trade",
    "say": "chat", "yell": "chat", "channel_say": "chat", "emote": "chat", "whisper": "whisper",
    "accept_quest": "quest", "complete_quest": "quest", "turn_in_quest": "quest",
    "abandon_quest": "quest",
    "use_item": "item", "destroy_item": "item_lost", "equip_item": "item", "compare_items": "item",
    "open_mailbox": "mail", "send_mail": "mail", "take_mail": "mail", "delete_mail": "mail",
    "move_to": "move", "move_towards": "move", "follow": "move", "stop_following": "move",
    "stop_movement": "move", "face": "move",
    "invite_to_group": "group", "accept_group": "group",
}
# ADR 0020 (#390): the activity kinds an agent may put in its audit line's `events`, with the detail
# keys each must carry as ints. Mirrors agent/audit_schema.py ACTIVITY_KINDS; any other kind is skipped.
AGENT_EVENT_KINDS = {"level": ("to",), "quest": ("quest_id",), "item": ("item_entry", "count"),
                     "money": ("copper",)}
# ADR 0020 (#390): the detail key that holds the fact of each kind, per source: `agent` as the agent
# writes it, `db` as the DB poller writes it (activity.py diff_snapshots). fact_key reads these, and the
# store writes its value to the fact_key column; the migration backfills level and quest (activity_schema.py,
# frozen). item has no backfill: its db rows from before the column keep fact_key NULL.
# A kind not listed here has no fact and no de-duplication.
# money has no fact: the DB money delta does not say its cause, so a loot or quest amount may show twice (the
# agent row and the DB delta row), and a sale of the same copper is kept. Ceiling, accepted (ADR 0020 slice 4).
# item ceiling, accepted: an item gained without SMSG_ITEM_PUSH_RESULT (mail take, MailHandler.cpp:499; trade,
# TradeHandler.cpp:158-198) within FACT_WINDOW_S of a push of the same entry loses its DB row.
FACT_KEYS = {"level": {"agent": "to", "db": "to"}, "quest": {"agent": "quest_id", "db": "quest"},
             "item": {"agent": "item_entry", "db": "item"}}


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


def _args_text(args):
    parts = [f"{k}={json.dumps(v, ensure_ascii=False) if isinstance(v, str) else v}"
             for k, v in (args or {}).items()]
    return " ".join(parts)


def audit_event(rec, agent):
    """UM-51 audit record -> feed event, or None for no-op cycles."""
    tc = rec.get("tool_call") or {}
    name = tc.get("name")
    if not name or name in SKIPPED_ACTIONS or rec.get("ts") is None:
        return None
    args = dict(tc.get("args") or {})
    msg = args.get("message", args.get("text"))
    if name == "say":
        text = f'Said "{msg}"'
    elif name == "yell":
        text = f'Yelled "{msg}"'
    elif name == "whisper":
        text = f'Whispered to {args.get("target_name")}: "{msg}"'
    elif name == "channel_say":
        text = f'[{args.get("channel")}] "{msg}"'
    elif name == "emote":
        text = f"Emote: {msg}"
    else:
        label = name.replace("_", " ").capitalize()
        text = f"{label} {_args_text(args)}".strip()
    result = rec.get("result") or {}
    ok = bool(result.get("ok")) and bool(rec.get("valid", True))
    if not ok:
        text += f" — failed: {result.get('error') or 'no result'}"
    detail = {"action": name, "args": args, "ok": ok, "error": result.get("error"),
              "cycle": rec.get("cycle")}
    return event(agent, rec["ts"], ACTION_KINDS.get(name, "action"), text, "audit",
                 f"audit:{agent}:{rec.get('cycle')}:{rec['ts']}", detail)


def money_text(copper):
    """1234567 -> '123g 45s 67c', dropping leading zero units ('5s 2c', '0c')."""
    c = abs(int(copper))
    g, s, cp = c // 10000, c // 100 % 100, c % 100
    parts = [f"{g}g"] if g else []
    if g or s:
        parts.append(f"{s}s")
    parts.append(f"{cp}c")
    return " ".join(parts)


def _agent_text(kind, detail, quest_title, item_title):
    # The same sentences the DB path writes for the fact (activity.py diff_snapshots).
    if kind == "level":
        return f"Reached level {detail['to']}"
    if kind == "quest":
        return f"Turned in quest: {quest_title(detail['quest_id'])}"
    if kind == "item":
        n = detail["count"]
        return f"Got {item_title(detail['item_entry'])}" + (f" ×{n}" if n > 1 else "")
    return f"Money +{money_text(detail['copper'])}"


def agent_events(rec, agent, quest_title=lambda q: f"Quest {q}", item_title=lambda e: f"Item {e}"):
    """The feed events of one audit line's `events` (ADR 0020, #390). Never raises: an element that
    is not a known kind with its typed detail keys is skipped (see skipped_agent_events).
    `quest_title` and `item_title` name a turn-in and a pickup (the DB names, when there is a DB)."""
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
        key = detail[AGENT_EVENT_KINDS[kind][0]]  # the uid's natural key: the first required detail key
        out.append(event(agent, t, kind, _agent_text(kind, detail, quest_title, item_title), "agent",
                         f"agent:{agent}:{kind}:{t:.3f}:{key}", detail))
    return out


def skipped_agent_events(rec, kept):
    """How many elements of rec["events"] agent_events() refused (`kept` is what it returned)."""
    raw = rec.get("events")
    if raw is None:
        return 0
    return 1 if not isinstance(raw, list) else len(raw) - len(kept)


def _fact_name(kind, source):
    """The detail key holding the fact of `kind` in an event of `source`, or None."""
    return FACT_KEYS.get(kind, {}).get(source)


def fact_key(e):
    """What a feed event states as a fact, for de-duplication (ADR 0020): (character, kind, its key
    value). None for an event with no fact (another kind, or a detail without the key)."""
    name = _fact_name(e.get("kind"), e.get("source"))
    value = (e.get("detail") or {}).get(name) if name else None
    if value is None:
        return None
    return (e["character"].lower(), e["kind"], value)


# The store's two rules (ADR 0020), on its connection. Every statement names the kind: a row of
# another kind with the same number is another fact, and is never matched or deleted.
# ponytail: a fixed window, not a per-kind one; the same value for every kind (ADR 0020 rule 7).
# DB lag is about 10 s (PlayerSaveInterval 5 s plus the 5 s poll) and clock skew is seconds. A fact
# repeated outside the window (a quest turned in again days later) is a second fact, not a duplicate.
FACT_WINDOW_S = 300


def fact_text(key):
    """The fact_key column of a row: the key's value as text (ADR 0020 slice 3), or None for no fact."""
    return None if key is None else str(key[2])


def agent_fact_stored(conn, key, t):
    """True when an agent row already holds the fact `key` within FACT_WINDOW_S of `t`: a db event of
    it at `t` is then not stored."""
    character, kind, _ = key
    return conn.execute(
        "SELECT 1 FROM events WHERE source = 'agent' AND kind = ? AND character = ?"
        " AND fact_key = ? AND ABS(t - ?) <= ? LIMIT 1",
        (kind, character, fact_text(key), t, FACT_WINDOW_S)).fetchone() is not None


def drop_db_fact(conn, key, t):
    """Delete the db rows of the fact `key` within FACT_WINDOW_S of `t`: an agent row for it at `t`
    has just been stored."""
    character, kind, _ = key
    conn.execute(
        "DELETE FROM events WHERE source = 'db' AND kind = ? AND character = ?"
        " AND fact_key = ? AND ABS(t - ?) <= ?",
        (kind, character, fact_text(key), t, FACT_WINDOW_S))
