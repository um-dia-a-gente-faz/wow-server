"""Per-character activity feed (UM-76): what a character did recently.

Three sources feed one small SQLite store (the last `KEEP_PER_CHARACTER` events per
character), read by `GET /api/character/<name>/activity`:

* ``db``    -- a ~5 s poller that snapshots every online character's money, level,
              zone, item counts and turned-in quests from the `characters` database
              and turns the differences into events. Causes the database does not
              record (a sale, a trade, a destroyed item) are *inferred* and flagged.
* ``chat``  -- tools/chat-feed's SSE stream. Only public kinds (say, yell, channel)
              are kept, even if chat-feed is ever configured to publish more.
* ``audit`` -- the agents' UM-51 decision logs (`<AUDIT_DIR>/<agent>/<day>.jsonl`),
              one event per decision. This is the persisted history; PR #113's
              "Agent mind" tab is the live view of the same records via the
              agent's own HTTP API, which only exists while the agent runs.

Everything here only reads MySQL, chat-feed and the audit files; the one thing it
writes is its own SQLite file.
"""
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request

log = logging.getLogger("wowmap.activity")

KEEP_PER_CHARACTER = 500
MAX_TEXT = 300
PUBLIC_CHAT_KINDS = frozenset({"say", "yell", "channel"})
# Decisions that mean "do nothing this cycle" -- not worth a feed row.
SKIPPED_ACTIONS = frozenset({"idle"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,
    character   TEXT NOT NULL COLLATE NOCASE,
    t           REAL NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL,
    inferred    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_character_t ON events (character, t);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _clip(text, limit=MAX_TEXT):
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def money_text(copper):
    """1234567 -> '123g 45s 67c', dropping leading zero units ('5s 2c', '0c')."""
    c = abs(int(copper))
    g, s, cp = c // 10000, c // 100 % 100, c % 100
    parts = [f"{g}g"] if g else []
    if g or s:
        parts.append(f"{s}s")
    parts.append(f"{cp}c")
    return " ".join(parts)


def event(character, t, kind, text, source, uid, detail=None, inferred=False):
    return {"character": character, "t": float(t), "kind": kind, "text": _clip(text),
            "detail": detail or {}, "source": source, "inferred": bool(inferred), "uid": uid}


# ---------------------------------------------------------------- store
class ActivityStore:
    """SQLite-backed event log, pruned to the newest `keep` events per character."""

    def __init__(self, path, keep=KEEP_PER_CHARACTER):
        if path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.keep = keep
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def add(self, events):
        """Insert events (duplicates by `uid` are ignored); returns how many were new."""
        if not events:
            return 0
        added = 0
        with self._lock:
            touched = set()
            for e in events:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO events (uid, character, t, kind, text, detail_json,"
                    " source, inferred) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (e["uid"], e["character"], e["t"], e["kind"], e["text"],
                     json.dumps(e.get("detail") or {}, default=str), e["source"],
                     1 if e.get("inferred") else 0))
                if cur.rowcount:
                    added += 1
                    touched.add(e["character"])
            for character in touched:
                self._conn.execute(
                    "DELETE FROM events WHERE character = ? AND id NOT IN ("
                    " SELECT id FROM events WHERE character = ? ORDER BY t DESC, id DESC LIMIT ?)",
                    (character, character, self.keep))
            self._conn.commit()
        return added

    def recent(self, character, limit=50):
        limit = max(1, min(int(limit), self.keep))
        with self._lock:
            rows = self._conn.execute(
                "SELECT t, kind, text, detail_json, source, inferred FROM events"
                " WHERE character = ? ORDER BY t DESC, id DESC LIMIT ?",
                (character, limit)).fetchall()
        return [{"t": t, "kind": kind, "text": text, "detail": json.loads(detail),
                 "source": source, "inferred": bool(inferred)}
                for t, kind, text, detail, source, inferred in rows]

    def count(self, character):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events WHERE character = ?",
                                      (character,)).fetchone()[0]

    def latest_t(self, character, source):
        with self._lock:
            row = self._conn.execute("SELECT MAX(t) FROM events WHERE character = ? AND source = ?",
                                     (character, source)).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_meta(self, key, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
            self._conn.commit()


# ---------------------------------------------------------------- database diff
# All tables/columns are from the TrinityCore 3.3.5 `characters` schema
# (sql/base/characters_database.sql): characters(guid, name, level, zone, money,
# online), character_inventory(guid, bag, slot, item), item_instance(guid, itemEntry,
# count), character_queststatus_rewarded(guid, quest, active).
SNAPSHOT_CHARACTERS_SQL = """
    SELECT guid, name, level, zone, money, online
    FROM characters.characters
    WHERE online = 1{extra}
"""
# Counts per item entry, wherever the item sits (equipped, bags, bank, keyring):
# equipping or banking an item changes its slot, not these totals.
SNAPSHOT_ITEMS_SQL = """
    SELECT ci.guid, ii.itemEntry, SUM(ii.count)
    FROM characters.character_inventory ci
    JOIN characters.item_instance ii ON ii.guid = ci.item
    WHERE ci.guid IN ({ids})
    GROUP BY ci.guid, ii.itemEntry
"""
SNAPSHOT_QUESTS_SQL = """
    SELECT guid, quest
    FROM characters.character_queststatus_rewarded
    WHERE guid IN ({ids})
"""
ITEM_NAMES_SQL = "SELECT entry, name, SellPrice FROM world.item_template WHERE entry IN ({ids})"
QUEST_NAMES_SQL = "SELECT ID, LogTitle FROM world.quest_template WHERE ID IN ({ids})"


def read_snapshot(conn, keep_guids=()):
    """One consistent read of every online character (plus `keep_guids`, so a
    character's last changes before logging out are still diffed).

    Three indexed queries inside one read-only REPEATABLE READ snapshot, so a
    Player::SaveToDB landing between them can't show money without the matching
    inventory change.
    """
    keep = sorted({int(g) for g in keep_guids})
    extra = f" OR guid IN ({','.join(map(str, keep))})" if keep else ""
    snap = {}
    with conn.cursor() as cur:
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        try:
            cur.execute(SNAPSHOT_CHARACTERS_SQL.format(extra=extra))
            for guid, name, level, zone, money, online in cur.fetchall():
                snap[int(guid)] = {"name": name, "level": int(level), "zone": int(zone or 0),
                                   "money": int(money), "online": bool(online),
                                   "items": {}, "quests": set()}
            if snap:
                ids = ",".join(map(str, sorted(snap)))
                cur.execute(SNAPSHOT_ITEMS_SQL.format(ids=ids))
                for guid, entry, count in cur.fetchall():
                    snap[int(guid)]["items"][int(entry)] = int(count)
                cur.execute(SNAPSHOT_QUESTS_SQL.format(ids=ids))
                for guid, quest in cur.fetchall():
                    snap[int(guid)]["quests"].add(int(quest))
        finally:
            cur.execute("COMMIT")
    return snap


def _counter_diff(old, new):
    gained, lost = {}, {}
    for entry in set(old) | set(new):
        d = new.get(entry, 0) - old.get(entry, 0)
        if d > 0:
            gained[entry] = d
        elif d < 0:
            lost[entry] = -d
    return gained, lost


def diff_snapshots(old, new, t, item_info=lambda e: (f"Item {e}", 0),
                   quest_title=lambda q: f"Quest {q}", zone_name=lambda z: f"zone {z}"):
    """Turn two snapshots ({guid: state}) into feed events.

    Facts: login/logout, level, zone, quests turned in, items gained, money.
    Inferred (flagged): an item that vanished while money went up is *sold*; one
    that vanished while another online character gained exactly the same item and
    count is *traded*; any other vanished item is *used or destroyed*; an item that
    appeared while money went down is *bought*. Items that vanish in a poll where a
    quest was turned in are attributed to the quest instead of a sale.
    `item_info(entry) -> (name, sell_price)`.
    """
    events = []
    stamp = f"{t:.3f}"

    def emit(guid, kind, text, detail=None, inferred=False, key=""):
        name = (new.get(guid) or old.get(guid))["name"]
        events.append(event(name, t, kind, text, "db", f"db:{guid}:{stamp}:{kind}:{key}",
                            detail, inferred))

    def qty(entry, n):
        return item_info(entry)[0] + (f" ×{n}" if n > 1 else "")

    changes = {}
    for guid, cur in new.items():
        prev = old.get(guid)
        if prev is None:
            if cur["online"]:
                emit(guid, "session", "Logged in")
            continue
        gained, lost = _counter_diff(prev["items"], cur["items"])
        changes[guid] = (gained, lost)

    # Trades: the same item and count leaving one character and reaching another.
    traded = {}
    for giver, (_, lost) in changes.items():
        for entry, n in lost.items():
            for taker, (gained, _) in changes.items():
                if taker != giver and gained.get(entry) == n and (taker, entry) not in traded.values():
                    traded[(giver, entry)] = (taker, entry)
                    break

    for guid, (gained, lost) in changes.items():
        prev, cur = old[guid], new[guid]
        if cur["level"] > prev["level"]:
            emit(guid, "level", f"Reached level {cur['level']}",
                 {"from": prev["level"], "to": cur["level"]})
        if cur["zone"] != prev["zone"] and cur["zone"]:
            emit(guid, "zone", f"Entered {zone_name(cur['zone'])}",
                 {"from": prev["zone"], "to": cur["zone"]})
        turned_in = sorted(cur["quests"] - prev["quests"])
        for q in turned_in:
            emit(guid, "quest", f"Turned in quest: {quest_title(q)}", {"quest": q}, key=str(q))

        dm = cur["money"] - prev["money"]
        for entry, n in sorted(lost.items()):
            name, sell_price = item_info(entry)
            detail = {"item": entry, "count": n}
            if (guid, entry) in traded:
                taker = traded[(guid, entry)][0]
                detail["to"] = new[taker]["name"]
                emit(guid, "trade", f"Gave {qty(entry, n)} to {new[taker]['name']}", detail, True, f"-{entry}")
            elif turned_in:
                emit(guid, "quest", f"Handed in {qty(entry, n)}", detail, True, f"-{entry}")
            elif dm > 0 and sell_price > 0:
                detail.update(sell_price=sell_price, money=dm)
                emit(guid, "sell", f"Sold {qty(entry, n)}", detail, True, f"-{entry}")
            else:
                emit(guid, "item_lost", f"Used or destroyed {qty(entry, n)}", detail, True, f"-{entry}")
        for entry, n in sorted(gained.items()):
            detail = {"item": entry, "count": n}
            giver = next((g for (g, e), (tk, _) in traded.items() if tk == guid and e == entry), None)
            if giver is not None:
                detail["from"] = old[giver]["name"]
                emit(guid, "trade", f"Received {qty(entry, n)} from {old[giver]['name']}", detail, True, f"+{entry}")
            elif dm < 0 and not turned_in:
                detail["money"] = dm
                emit(guid, "buy", f"Bought {qty(entry, n)}", detail, True, f"+{entry}")
            else:
                emit(guid, "item", f"Got {qty(entry, n)}", detail, key=f"+{entry}")
        if dm:  # always a fact of its own, even when a sale/purchase above explains it
            sign = "+" if dm > 0 else "−"
            emit(guid, "money", f"Money {sign}{money_text(dm)}", {"delta": dm, "total": cur["money"]})

        if prev["online"] and not cur["online"]:
            emit(guid, "session", "Logged out")
    return events


class DbPoller:
    """Polls the characters database and stores diff events."""

    def __init__(self, store, connect, zone_name, interval=5.0):
        self.store = store
        self.connect = connect
        self.zone_name = zone_name
        self.interval = interval
        self.snapshot = None
        self._items = {}
        self._quests = {}
        self.last_ok = None
        self.last_error = None

    def _lookup(self, conn, cache, sql, ids, convert):
        missing = sorted(i for i in ids if i not in cache)
        if missing:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.format(ids=",".join(map(str, missing))))
                    for row in cur.fetchall():
                        cache[int(row[0])] = convert(row)
            except Exception as e:  # noqa: BLE001 - names are cosmetic
                log.warning("activity name lookup failed: %s", e)
        return cache

    def poll_once(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as conn:
            prev = self.snapshot or {}
            new = read_snapshot(conn, keep_guids=[g for g, s in prev.items() if s["online"]])
            if self.snapshot is None:  # first poll after start: baseline only
                self.snapshot = {g: s for g, s in new.items() if s["online"]}
                return []
            entries, quests = set(), set()
            for g, s in new.items():
                if g in prev:
                    gained, lost = _counter_diff(prev[g]["items"], s["items"])
                    entries |= set(gained) | set(lost)
                    quests |= s["quests"] - prev[g]["quests"]
            self._lookup(conn, self._items, ITEM_NAMES_SQL, entries,
                         lambda r: (r[1] or f"Item {r[0]}", int(r[2] or 0)))
            self._lookup(conn, self._quests, QUEST_NAMES_SQL, quests,
                         lambda r: r[1] or f"Quest {r[0]}")
        events = diff_snapshots(
            prev, new, now,
            item_info=lambda e: self._items.get(e, (f"Item {e}", 0)),
            quest_title=lambda q: self._quests.get(q, f"Quest {q}"),
            zone_name=self.zone_name)
        self.snapshot = {g: s for g, s in new.items() if s["online"]}
        self.store.add(events)
        return events

    def run(self):
        while True:
            try:
                self.poll_once()
                self.last_ok, self.last_error = time.time(), None
            except Exception as e:  # noqa: BLE001 - keep polling through DB hiccups
                self.last_error = str(e)
                log.warning("activity db poll failed: %s", e)
            time.sleep(self.interval)


# ---------------------------------------------------------------- chat
def _parse_iso(at):
    try:
        return dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


def chat_event(ev, skip_senders=frozenset()):
    """chat-feed SSE event -> feed event, or None for anything not public."""
    kind, sender = ev.get("kind"), ev.get("sender")
    if kind not in PUBLIC_CHAT_KINDS or not sender or not ev.get("id"):
        return None
    if sender.lower() in skip_senders:  # agents: their audit log already has it
        return None
    text = ev.get("text") or ""
    if kind == "say":
        line = f'Said "{text}"'
    elif kind == "yell":
        line = f'Yelled "{text}"'
    else:
        line = f'[{ev.get("channel") or "channel"}] "{text}"'
    return event(sender, _parse_iso(ev.get("at")), "chat", line, "chat", f"chat:{ev['id']}",
                 {"kind": kind, "channel": ev.get("channel")})


def iter_sse(lines):
    """Yield (event_name, id, data) from an iterable of decoded SSE lines."""
    name, eid, data = "message", None, []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield name, eid, "\n".join(data)
            name, eid, data = "message", None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "id":
            eid = value
        elif field == "data":
            data.append(value)


class ChatFollower:
    """Follows chat-feed's SSE stream; resumes with Last-Event-ID across restarts."""

    META_KEY = "chat_last_id"

    def __init__(self, store, url, agents=lambda: frozenset()):
        self.store = store
        self.url = url.rstrip("/") + "/api/chat/stream"
        self.agents = agents
        self.connected = False
        self.last_error = None

    def handle(self, eid, data):
        try:
            ev = json.loads(data)
        except ValueError:
            return None
        e = chat_event(ev, self.agents())
        if e:
            self.store.add([e])
        if eid:
            self.store.set_meta(self.META_KEY, eid)
        return e

    def run(self):
        backoff = 2
        while True:
            req = urllib.request.Request(self.url, headers={"Accept": "text/event-stream"})
            last = self.store.get_meta(self.META_KEY)
            if last:
                req.add_header("Last-Event-ID", last)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.connected, self.last_error, backoff = True, None, 2
                    lines = (b.decode("utf-8", "replace") for b in r)
                    for name, eid, data in iter_sse(lines):
                        if name == "chat":
                            self.handle(eid, data)
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                self.last_error = str(e)
                log.info("activity chat stream: %s", e)
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------- agent audit log
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
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(\.\d+)?$")


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


class AuditTailer:
    """Reads new lines from every agent's audit files (tolerates rotation)."""

    def __init__(self, store, base_dir, interval=5.0):
        self.store = store
        self.base_dir = base_dir
        self.interval = interval
        self._offsets = {}   # (dev, ino) -> byte offset of the next unread line
        self._since = {}     # agent -> newest ts already stored
        self.agents = frozenset()

    def agent_names(self):
        try:
            return sorted(d for d in os.listdir(self.base_dir)
                          if os.path.isdir(os.path.join(self.base_dir, d)))
        except OSError:
            return []

    def _files(self, agent, since):
        """Day files (including size-rotated `.N` ones) not older than `since`'s day."""
        floor = time.strftime("%Y-%m-%d", time.localtime(since)) if since else \
            time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        directory = os.path.join(self.base_dir, agent)
        out = []
        for fn in os.listdir(directory):
            m = _DAY_FILE_RE.match(fn)
            if m and m.group(1) >= floor:
                out.append(os.path.join(directory, fn))
        return out

    def scan_once(self):
        added = []
        names = self.agent_names()
        self.agents = frozenset(n.lower() for n in names)
        for agent in names:
            if agent not in self._since:
                self._since[agent] = self.store.latest_t(agent, "audit") or 0
            since = self._since[agent]
            events = []
            try:
                files = self._files(agent, since)
            except OSError:
                continue
            for path in files:
                try:
                    st = os.stat(path)
                    key = (st.st_dev, st.st_ino)
                    offset = self._offsets.get(key, 0)
                    if st.st_size < offset:
                        offset = 0
                    if st.st_size == offset:
                        continue
                    with open(path, "rb") as f:
                        f.seek(offset)
                        chunk = f.read()
                except OSError:
                    continue
                end = chunk.rfind(b"\n") + 1  # leave a half-written last line for later
                self._offsets[key] = offset + end
                for line in chunk[:end].splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or (rec.get("ts") or 0) <= since:
                        continue
                    e = audit_event(rec, agent)
                    if e:
                        events.append(e)
            if events:
                self.store.add(events)
                self._since[agent] = max(since, max(e["t"] for e in events))
                added.extend(events)
        return added

    def run(self):
        while True:
            try:
                self.scan_once()
            except Exception as e:  # noqa: BLE001
                log.warning("activity audit scan failed: %s", e)
            time.sleep(self.interval)


# ---------------------------------------------------------------- wiring
class Activity:
    """Owns the store and the three background sources."""

    def __init__(self, store, connect=None, zone_name=None, chat_url="", audit_dir=""):
        self.store = store
        self.audit = AuditTailer(store, audit_dir) if audit_dir else None
        self.chat = ChatFollower(store, chat_url, self.agent_names) if chat_url else None
        self.db = DbPoller(store, connect, zone_name) if connect else None

    def agent_names(self):
        return self.audit.agents if self.audit else frozenset()

    def start(self):
        for source in (self.db, self.chat, self.audit):
            if source is not None:
                threading.Thread(target=source.run, daemon=True,
                                 name=f"activity-{type(source).__name__}").start()

    def status(self):
        return {
            "db": None if self.db is None else {"ok": self.db.last_error is None,
                                                 "error": self.db.last_error},
            "chat": None if self.chat is None else {"ok": self.chat.connected,
                                                     "error": self.chat.last_error},
            "audit": None if self.audit is None else {"ok": os.path.isdir(self.audit.base_dir)},
        }

    def feed(self, character, limit=50):
        return {"character": character,
                "agent": character.lower() in self.agent_names(),
                "events": self.store.recent(character, limit),
                "sources": self.status()}
