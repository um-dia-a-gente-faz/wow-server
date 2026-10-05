import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import activity  # noqa: E402
import app  # noqa: E402
import pagesrc  # noqa: E402
import routes  # noqa: E402
from webio import Request  # noqa: E402
import state as wowmap_state  # noqa: E402

ITEMS = {2589: ("Linen Cloth", 13), 159: ("Refreshing Spring Water", 1), 6948: ("Hearthstone", 0)}


def info(entry):
    return ITEMS.get(entry, (f"Item {entry}", 0))


def state(name, level=10, zone=3430, money=1000, items=None, quests=(), online=True):
    return {"name": name, "level": level, "zone": zone, "money": money, "online": online,
            "items": dict(items or {}), "quests": set(quests)}


def kinds(events, who=None):
    return [(e["kind"], e["text"], e["inferred"]) for e in events
            if who is None or e["character"] == who]


class DiffTests(unittest.TestCase):
    def diff(self, old, new):
        return activity.diff_snapshots(old, new, 1000.0, item_info=info,
                                       quest_title=lambda q: f"Q{q}", zone_name=lambda z: f"Z{z}")

    def test_loot_sale_level_zone_and_quest(self):
        old = {7: state("Rubens", items={2589: 2}, quests={1})}
        new = {7: state("Rubens", level=11, zone=3433, items={2589: 2, 159: 5}, quests={1})}
        self.assertEqual(kinds(self.diff(old, new)), [
            ("level", "Reached level 11", False),
            ("zone", "Entered Z3433", False),
            ("item", "Got Refreshing Spring Water ×5", False),
        ])
        sold = {7: state("Rubens", money=1026, items={})}
        ev = self.diff({7: state("Rubens", items={2589: 2})}, sold)
        self.assertEqual(kinds(ev), [("sell", "Sold Linen Cloth ×2", True),
                                     ("money", "Money +26c", False)])

    def test_unsellable_item_vanishing_is_not_a_sale(self):
        ev = self.diff({7: state("Rubens", items={6948: 1})},
                       {7: state("Rubens", money=1500, items={})})
        self.assertEqual(ev[0]["kind"], "item_lost")
        self.assertTrue(ev[0]["inferred"])

    def test_trade_between_two_online_characters(self):
        old = {1: state("Rubens", items={2589: 3}), 2: state("Luaprata", items={})}
        new = {1: state("Rubens", items={}), 2: state("Luaprata", items={2589: 3})}
        ev = self.diff(old, new)
        self.assertEqual(kinds(ev, "Rubens"), [("trade", "Gave Linen Cloth ×3 to Luaprata", True)])
        self.assertEqual(kinds(ev, "Luaprata"), [("trade", "Received Linen Cloth ×3 from Rubens", True)])

    def test_quest_turn_in_hands_items_in_instead_of_selling(self):
        ev = self.diff({7: state("Rubens", items={2589: 2})},
                       {7: state("Rubens", money=5000, items={159: 1}, quests={42})})
        self.assertEqual([k for k, _, _ in kinds(ev)], ["quest", "quest", "item", "money"])
        self.assertEqual(ev[0]["text"], "Turned in quest: Q42")
        self.assertEqual(ev[1]["text"], "Handed in Linen Cloth ×2")

    def test_purchase_and_login_logout(self):
        ev = self.diff({7: state("Rubens", items={})},
                       {7: state("Rubens", money=900, items={159: 1}, online=False),
                        8: state("Farstrider")})
        self.assertEqual(kinds(ev, "Farstrider"), [("session", "Logged in", False)])
        self.assertEqual(kinds(ev, "Rubens"), [("buy", "Bought Refreshing Spring Water", True),
                                               ("money", "Money −1s 0c", False),
                                               ("session", "Logged out", False)])

    def test_moving_items_between_slots_is_silent(self):
        same = {7: state("Rubens", items={2589: 2})}
        self.assertEqual(self.diff(same, {7: state("Rubens", items={2589: 2})}), [])

    def test_money_text(self):
        self.assertEqual(activity.money_text(0), "0c")
        self.assertEqual(activity.money_text(502), "5s 2c")
        self.assertEqual(activity.money_text(-1234567), "123g 45s 67c")


class StoreTests(unittest.TestCase):
    def test_dedupes_prunes_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "a.sqlite3")
            store = activity.ActivityStore(path, keep=5)
            evs = [activity.event("Rubens", i, "money", f"m{i}", "db", f"u{i}") for i in range(8)]
            self.assertEqual(store.add(evs), 8)
            self.assertEqual(store.add(evs[-2:]), 0)  # same uid, ignored
            store.add([activity.event("Other", 1, "chat", "hi", "chat", "o1")])
            reopened = activity.ActivityStore(path, keep=5)
            got = reopened.recent("rubens", 50)  # name lookup is case-insensitive
            self.assertEqual([e["text"] for e in got], ["m7", "m6", "m5", "m4", "m3"])
            self.assertEqual(reopened.count("Other"), 1)
            self.assertEqual(reopened.latest_t("Rubens", "db"), 7)


class ChatTests(unittest.TestCase):
    def test_only_public_chat_is_kept(self):
        base = {"id": "1:2", "at": "2026-10-02T12:00:00Z", "sender": "Rubens", "text": "hi"}
        for kind in ("whisper", "party", "guild", "raid", "officer", "unknown"):
            self.assertIsNone(activity.chat_event({**base, "kind": kind}), kind)
        say = activity.chat_event({**base, "kind": "say"})
        self.assertEqual((say["character"], say["text"], say["uid"]), ("Rubens", 'Said "hi"', "chat:1:2"))
        chan = activity.chat_event({**base, "kind": "channel", "channel": "General"})
        self.assertEqual(chan["text"], '[General] "hi"')
        self.assertIsNone(activity.chat_event({**base, "kind": "say"}, frozenset({"rubens"})))

    def test_sse_parsing_and_resume_id(self):
        lines = [": keepalive", "", "id: 9:1", "event: chat",
                 'data: {"id":"9:1","kind":"say","sender":"Rubens","text":"a"}', "",
                 "id: 9:2", "event: chat",
                 'data: {"id":"9:2","kind":"whisper","sender":"Rubens","text":"secret"}', ""]
        store = activity.ActivityStore(":memory:")
        follower = activity.ChatFollower(store, "http://x:9500")
        for name, eid, data in activity.iter_sse(lines):
            self.assertEqual(name, "chat")
            follower.handle(eid, data)
        self.assertEqual([e["text"] for e in store.recent("Rubens")], ['Said "a"'])
        self.assertEqual(store.get_meta(follower.META_KEY), "9:2")


class AuditTests(unittest.TestCase):
    def rec(self, ts, cycle, name, args=None, ok=True, error=None):
        return {"ts": ts, "agent": "Luaprata", "cycle": cycle, "valid": True,
                "tool_call": {"name": name, "args": args or {}}, "result": {"ok": ok, "error": error}}

    def test_mapping(self):
        e = activity.audit_event(self.rec(1, 1, "whisper", {"target_name": "Rubens", "message": "hey"}), "Luaprata")
        self.assertEqual((e["kind"], e["text"]), ("whisper", 'Whispered to Rubens: "hey"'))
        e = activity.audit_event(self.rec(1, 2, "auto_attack", {"guid": 5}, ok=False, error="out of range"), "L")
        self.assertEqual((e["kind"], e["text"]), ("combat", "Auto attack guid=5 — failed: out of range"))
        self.assertEqual(activity.audit_event(self.rec(1, 3, "loot", {"guid": 9}), "L")["kind"], "loot")
        self.assertIsNone(activity.audit_event(self.rec(1, 4, "idle"), "L"))
        self.assertIsNone(activity.audit_event({"ts": 1, "tool_call": {"name": None}}, "L"))

    def test_tailer_reads_new_lines_survives_rotation_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            agent_dir = os.path.join(d, "Luaprata")
            os.makedirs(agent_dir)
            day = time.strftime("%Y-%m-%d")
            path = os.path.join(agent_dir, f"{day}.jsonl")
            now = time.time()

            def write(recs, p=path):
                with open(p, "a", encoding="utf-8") as f:
                    for r in recs:
                        f.write(json.dumps(r) + "\n")

            write([self.rec(now, 1, "loot", {"guid": 1}), self.rec(now + 1, 2, "idle")])
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"ts": ')  # half-written line must wait
            store = activity.ActivityStore(":memory:")
            tailer = activity.AuditTailer(store, d)
            self.assertEqual(len(tailer.scan_once()), 1)
            self.assertEqual(tailer.agents, frozenset({"luaprata"}))
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({**self.rec(now + 2, 3, "sell_item", {"slot": 4})})[len('{"ts": '):] + "\n")
            self.assertEqual([e["kind"] for e in tailer.scan_once()], ["sell"])
            os.rename(path, path + ".1")  # size rotation
            write([self.rec(now + 3, 4, "say", {"message": "hello"})])
            self.assertEqual([e["text"] for e in tailer.scan_once()], ['Said "hello"'])
            # A fresh tailer (wowmap restart) only adds what is newer than the store.
            self.assertEqual(activity.AuditTailer(store, d).scan_once(), [])
            self.assertEqual(store.count("Luaprata"), 3)


class FakeCursor:
    def __init__(self, data):
        self.data, self.rows = data, []

    def execute(self, sql, args=None):
        self.data["sql"].append(sql)
        if "FROM characters.characters" in sql:
            self.rows = self.data["chars"]
        elif "character_inventory" in sql:
            self.rows = self.data["items"]
        elif "queststatus_rewarded" in sql:
            self.rows = self.data["quests"]
        elif "item_template" in sql:
            self.rows = [(2589, "Linen Cloth", 13)]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, data):
        self.data = data

    def cursor(self):
        return FakeCursor(self.data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PollerTests(unittest.TestCase):
    def test_baseline_then_diff_in_one_consistent_snapshot(self):
        data = {"sql": [], "chars": [(7, "Rubens", 10, 3430, 100, 1)],
                "items": [(7, 2589, 2)], "quests": []}
        store = activity.ActivityStore(":memory:")
        poller = activity.DbPoller(store, lambda: FakeConn(data), lambda z: f"Z{z}")
        self.assertEqual(poller.poll_once(now=1), [])
        self.assertIn("CONSISTENT SNAPSHOT, READ ONLY", data["sql"][0])
        self.assertEqual(data["sql"][-1], "COMMIT")
        data["chars"], data["items"] = [(7, "Rubens", 10, 3430, 126, 1)], []
        evs = poller.poll_once(now=2)
        self.assertEqual([e["text"] for e in evs], ["Sold Linen Cloth ×2", "Money +26c"])
        self.assertIn("OR guid IN (7)", next(s for s in data["sql"] if "characters.characters" in s
                                             and "OR guid" in s))
        self.assertEqual(store.count("Rubens"), 2)


class ApiTests(unittest.TestCase):
    def test_activity_route_and_page(self):
        store = activity.ActivityStore(":memory:")
        store.add([activity.event("Rubens", 5, "chat", 'Said "hi"', "chat", "c1")])
        feed = activity.Activity(store)
        def get(path):
            return routes.dispatch(Request("GET", path, {"limit": ["10"]}, {}, None))
        with mock.patch.object(wowmap_state, "activity", feed):
            code, body = get("/api/character/Rubens/activity")[:2]
        self.assertEqual(code, 200)
        self.assertEqual(body["events"][0]["text"], 'Said "hi"')
        with mock.patch.object(wowmap_state, "activity", None):
            self.assertEqual(get("/api/character/Rubens/activity")[0], 503)
        self.assertNotIn("@activity-", pagesrc.PAGE)
        self.assertIn("window.ActivityFeed", pagesrc.PAGE)
        self.assertIn("Recent activity", pagesrc.PAGE)

    def test_activity_has_its_own_drawer_tab(self):
        # #210: the feed lives in its own tab pane, not stacked in the character body.
        self.assertIn("'Activity'", pagesrc.PAGE)
        self.assertIn("act-pane", pagesrc.PAGE)
        self.assertNotIn("ActivityFeed.section(c.name)", pagesrc.PAGE)


# Minimal DOM stand-in: enough for ActivityFeed to render, then dump every row's
# text with each <mark> shown as [..].
FEED_HARNESS = r"""
const node = (tag) => ({tag, children: [], className: '', style: {}, value: '',
  set textContent(v) { this.children = [String(v)]; },
  append(...k) { this.children.push(...k); }, replaceChildren(f) { this.children = f.children; },
  setAttribute() {}, focus() {}, setSelectionRange() {}});
globalThis.document = {createElement: node, createDocumentFragment: () => node('#frag'),
  addEventListener() {}, hidden: true};
globalThis.window = globalThis;
globalThis.setInterval = () => 0;
globalThis.ago = () => '1m ago';
globalThis.Inspect = {current: () => 'Rubens'};
globalThis.fetch = async () => ({ok: true, json: async () => ({sources: {}, events: EVENTS})});
const txt = (n) => typeof n === 'string' ? n
  : n.tag === 'mark' ? '[' + n.children.join('') + ']' : n.children.map(txt).join('');
//SCRIPT//
(async () => {
  const box = window.ActivityFeed.section('Rubens');
  await new Promise((r) => setTimeout(r, 0));
  const [, search, list] = box.children;
  search.value = QUERY;
  search.oninput();
  console.log(JSON.stringify(list.children.filter((r) => r.className.startsWith('act')).map(txt)));
})();
"""


def feed_rows(events, query):
    js = (FEED_HARNESS.replace("EVENTS", json.dumps(events)).replace("QUERY", json.dumps(query))
          .replace("//SCRIPT//", pagesrc.ACTIVITY_JS.replace("<script>", "").replace("</script>", "")))
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class FeedUiTests(unittest.TestCase):
    """#173: timestamps, search and scrolling in the drawer's Recent activity."""
    js, css = pagesrc.ACTIVITY_JS, pagesrc.ACTIVITY_CSS

    def test_absolute_and_relative_time(self):
        self.assertIn("const clock = ", self.js)
        self.assertIn("${clock(e.t)} · ${ago(e.t)}", self.js)

    def test_timer_only_while_tab_visible(self):
        self.assertIn("if (!document.hidden) refresh()", self.js)
        self.assertIn("visibilitychange", self.js)

    def test_search_box_is_its_own_and_filters_client_side(self):
        self.assertIn("'act-search'", self.js)
        self.assertIn(".activity .act-search", self.css)
        self.assertIn("search.oninput = render", self.js)
        self.assertIn("no matches", self.js)
        self.assertIn("el('mark', null", self.js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_every_search_match_is_highlighted(self):
        ev = {"t": 1, "kind": "trade", "text": "Gave Linen Cloth to Bob", "source": "db",
              "detail": {"item": 2589, "to": "Bob"}}
        self.assertIn("[trade]", feed_rows([ev], "trade")[0])
        self.assertIn("[database]", feed_rows([ev], "database")[0])
        self.assertIn("to [Bob]", feed_rows([ev], "bob")[0])
        self.assertEqual(feed_rows([ev], "2589"), [])  # hidden detail values can't be highlighted

    def test_list_scrolls_and_says_how_far_back(self):
        self.assertRegex(self.css, r"\.activity \.act-list \{[^}]*overflow-y:auto")
        self.assertIn("back to", self.js)

    def test_untrusted_text_never_goes_through_innerhtml(self):
        self.assertNotIn("innerHTML", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)


if __name__ == "__main__":
    unittest.main()
