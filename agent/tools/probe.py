#!/usr/bin/env python3
"""Agent capability probe (#139): does this agent actually work, step by step?

Usage: python3 -m agent.tools.probe --confirm-manual-run [options]

MANUAL ONLY. Never schedule it, never call it from CI or an agent loop, never
run it against a character while a counted run is in progress. The probe walks,
attacks, talks and accepts a quest as the character; by the definition in
docs/AGENT-RUN-1-10.md ("Manually sending any packet or chat command on the
agent's behalf") that is a human intervention, and it fails a counted run the
moment it happens. Use a character that is not in a counted run.

Steps, in order, each reported ok / fail / skipped with the raw action result
and its duration:

  login   auth + world login as the configured character (WOW_* env, as `python3 -m agent`)
  chat    say a standard line (agent/lines.py), confirmed by the chat-feed, not by the agent
  move    move_towards Magistrix Erona
  quest   interact + accept_quest on Erona, quest 8325 (confirmed in the quest log)
  combat  set_target + auto_attack a level-1 mob (damage dealt, did it die)
  loot    loot a corpse, only if one is available
  rest    rest, report health/mana regained

A step whose fixtures are not there (character elsewhere, NPC or mob not in
view, quest already taken) is "skipped" with a reason; the probe never walks to
a guessed position. Fixtures live in agent/known_targets.py.

Output is a compact text report, or the full machine-readable JSON report with
--json; --out FILE also writes the JSON (the console panel will read the last
run from there, see load_last_report(): a missing file means "never probed").
Exit status: 0 no step failed, 1 a step failed, 2 usage or config error.
No GM commands, no account or database changes, never the owner's character.
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import textwrap
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from .. import actions as ac
from .. import known_targets as kt
from .. import lines
from .. import npc
from ..reflexes import follow as _follow  # noqa: F401 -- importing registers follow/assist
from ..reflexes import rest as restmod

REPORT_SCHEMA = 1
STEP_NAMES = ("chat", "move", "quest", "combat", "loot", "rest")

MANUAL_ONLY_NOTICE = (
    "MANUAL ONLY: the probe walks, attacks and talks as the character. Under "
    "docs/AGENT-RUN-1-10.md that is a human intervention, so never run it "
    "during a counted run, never schedule it, never trigger it automatically.")

MOVE_STOP_YD = 3.0         # close enough to talk (5 yd) and to melee (5 yd) with margin
LOOT_WALK_YD = 40.0        # farthest corpse worth walking to
MIN_FIGHT_HEALTH_PCT = 50  # don't pick a fight below this
ABORT_FIGHT_HEALTH_PCT = 30


# ── Results ──────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    """What one step decided. `raw` is the underlying action result (or a list
    of them), kept so the report shows exactly what the action layer said."""
    status: str                      # "ok" | "fail" | "skipped"
    summary: str = ""
    reason: str | None = None        # why skipped
    error: str | None = None         # why failed
    detail: dict = field(default_factory=dict)
    raw: object = None


def ok(summary: str, raw=None, **detail) -> Outcome:
    return Outcome("ok", summary=summary, detail=detail, raw=raw)


def fail(error: str, raw=None, **detail) -> Outcome:
    return Outcome("fail", summary=error, error=error, detail=detail, raw=raw)


def skip(reason: str) -> Outcome:
    return Outcome("skipped", summary=reason, reason=reason)


def raw_result(res) -> dict:
    """An ActionResult as plain data."""
    return {"ok": res.ok, "error": res.error, "detail": res.detail}


@dataclass
class Timeouts:
    chat_s: float = 10.0       # wait for our line to show up in the chat-feed
    window_s: float = 5.0      # wait for the NPC's gossip/quest window
    quest_s: float = 5.0       # wait for the quest to land in the quest log
    fight_s: float = 45.0      # whole fight
    corpse_s: float = 3.0      # wait for the corpse to become lootable
    rest_s: float = 120.0      # whole rest
    poll_s: float = 0.3


@dataclass
class Env:
    """Everything a step touches, so tests can hand in fakes."""
    session: object
    world: object
    actions: dict
    character: str
    chat: object | None = None     # ChatFeedWatcher-like: start/mark/wait_for/stop
    line_id: str = lines.PROBE_LINE_ID
    timeouts: Timeouts = field(default_factory=Timeouts)
    clock: object = time.monotonic
    sleep: object = time.sleep
    shared: dict = field(default_factory=dict)  # step -> step hand-over (killed mob guid)

    def wait_until(self, predicate, timeout: float):
        """Poll predicate() until it returns something truthy (returned) or the
        timeout passes (None)."""
        deadline = self.clock() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if self.clock() >= deadline:
                return None
            self.sleep(self.timeouts.poll_s)

    def act(self, name: str, **params):
        action = self.actions.get(name)
        if action is None:
            return ac.ActionResult(ok=False, error=f"action {name!r} is not available")
        return action.run(self.session, self.world, **params)


def default_actions() -> dict:
    """REGISTRY plus `say`: chat actions are deliberately not registered for the
    brain (UM-98), but the probe is a human-driven tool and needs one."""
    return {**ac.REGISTRY, "say": ac.SayAction()}


# ── Chat-feed oracle ─────────────────────────────────────────────────────

class ChatFeedError(RuntimeError):
    pass


def parse_sse(lines_iter):
    """Yield (event, data) for each complete SSE frame in an iterable of byte or
    str lines. Comments (keepalives) and `id:` lines are ignored."""
    event, data = "message", []
    for raw in lines_iter:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


class ChatFeedWatcher:
    """Reads tools/chat-feed's SSE stream (/api/chat/stream) in a background
    thread. The feed tails the server's own chat log, so a line seen here was
    heard by the *server*, whatever the agent believes it sent.

    The stream replays recent history on connect, so callers take mark() just
    before sending and wait_for() only looks at events that arrived after it."""

    def __init__(self, base_url: str, token: str = "", opener=urllib.request.urlopen,
                 timeout: float = 30.0,  # socket timeout; the feed sends a keepalive every ~15 s
                 settle_s: float = 1.0, sleep=time.sleep, clock=time.monotonic):
        self.url = base_url.rstrip("/") + "/api/chat/stream"
        self._token = token  # never logged or reported
        self._opener = opener
        self._timeout = timeout
        self._mark = 0
        self._settle_s = settle_s
        self._sleep = sleep
        self._clock = clock
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._resp = None
        self._thread = None
        self.error: str | None = None

    def start(self):
        """Connect (raises ChatFeedError if it can't), then wait out the replay."""
        headers = {"Accept": "text/event-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self.url, headers=headers)
        try:
            self._resp = self._opener(req, timeout=self._timeout)
        except Exception as e:
            raise ChatFeedError(f"cannot open {self.url}: {type(e).__name__}") from e
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self._sleep(self._settle_s)

    def _pump(self):
        try:
            for event, data in parse_sse(self._resp):
                if event != "chat":
                    continue
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    with self._lock:
                        self.events.append(parsed)
        except Exception as e:  # stream dropped; wait_for() just times out
            self.error = f"{type(e).__name__}: {e}"

    def mark(self) -> int:
        with self._lock:
            self._mark = len(self.events)
            return self._mark

    def wait_for(self, sender: str, text: str, kind: str = "say", timeout: float = 10.0) -> dict | None:
        """The first event after mark() from `sender` (case-insensitive) of `kind`
        whose text is exactly `text`, or None after `timeout` seconds."""
        deadline = self._clock() + timeout
        while True:
            with self._lock:
                fresh = list(self.events[self._mark:])
            for e in fresh:
                if (e.get("kind") == kind and (e.get("sender") or "").casefold() == sender.casefold()
                        and e.get("text") == text):
                    return e
            if self._clock() >= deadline:
                return None
            self._sleep(0.2)

    def stop(self):
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


# ── Perception helpers ───────────────────────────────────────────────────

def _health_pct(obj) -> float | None:
    if obj is None or obj.health is None or not obj.max_health:
        return None
    return 100.0 * obj.health / obj.max_health


def _power_pct(obj) -> float | None:
    if obj is None or obj.power_type != 0:  # POWER_MANA
        return None
    cur, mx = obj.power.get("mana"), obj.max_power.get("mana")
    if cur is None or not mx:
        return None
    return 100.0 * cur / mx


def _distance(env: Env, obj) -> float | None:
    if obj is None or env.session.player_position is None:
        return None
    return obj.distance_to(env.session.player_position)


def _area_skip(env: Env) -> str | None:
    pos = env.session.player_position
    if pos is None:
        return "own position unknown"
    if pos[0] != kt.START_AREA_MAP_ID:
        return (f"character is on map {pos[0]}, fixtures are on map {kt.START_AREA_MAP_ID} "
                f"({kt.START_AREA_NAME})")
    return None


def _find_units(env: Env, names, *, level: int | None = None, alive: bool = False,
                max_yd: float = kt.MAX_WALK_YD) -> list:
    """Perceived units whose name is exactly one of `names`, nearest first, within
    `max_yd`. With `alive`, dead or health-unknown units are dropped."""
    wanted = {n.casefold() for n in names}
    found = []
    for obj in env.world.get_objects().values():
        if obj.object_type != "unit" or obj.name.casefold() not in wanted:
            continue
        if level is not None and obj.level != level:
            continue
        if alive and not (obj.health and obj.health > 0):
            continue
        d = _distance(env, obj)
        if d is not None and d <= max_yd:
            found.append((d, obj))
    return [obj for _d, obj in sorted(found, key=lambda t: t[0])]


def _events_since(env: Env, t0: float, kind: str):
    return [e for e in list(env.session.events) if e.get("kind") == kind and e.get("t", 0) >= t0]


# ── Steps ────────────────────────────────────────────────────────────────

def step_chat(env: Env) -> Outcome:
    try:
        text = lines.resolve_line(env.line_id)
    except lines.LineError as e:
        return fail(str(e))
    if env.chat is None:
        return fail("no chat-feed configured, so the line cannot be confirmed (use --skip chat to leave it out)")
    if not env.character:
        return fail("own character name unknown, cannot match the chat-feed sender")
    try:
        env.chat.start()
    except ChatFeedError as e:
        return fail(f"unconfirmed: chat-feed unreachable ({e})")
    env.chat.mark()
    res = env.act("say", message=text)
    raw = raw_result(res)
    if not res.ok:
        return fail(res.error or "say failed", raw=raw, line=text)
    heard = env.chat.wait_for(env.character, text, kind="say", timeout=env.timeouts.chat_s)
    if heard is None:
        return fail(f"say sent but not seen in the chat-feed within {env.timeouts.chat_s:.0f}s",
                    raw=raw, line=text, chat_feed_error=getattr(env.chat, "error", None))
    return ok(f"said {env.line_id!r}, seen in chat-feed", raw=raw, line_id=env.line_id, line=text,
              feed_event=heard)


def step_move(env: Env) -> Outcome:
    why = _area_skip(env)
    if why:
        return skip(why)
    giver = _find_units(env, [kt.QUEST_GIVER.name])
    if not giver:
        return skip(f"{kt.QUEST_GIVER.name} not in view within {kt.MAX_WALK_YD:.0f} yd")
    target = giver[0]
    before = _distance(env, target)
    if before is not None and before <= MOVE_STOP_YD + 1.0:
        return skip(f"already {before:.1f} yd from {kt.QUEST_GIVER.name}, nothing to walk")
    res = env.act("move_towards", guid=target.guid, stop_distance=MOVE_STOP_YD)
    raw = raw_result(res)
    after = _distance(env, env.world.get_object(target.guid))
    closed = (before - after) if before is not None and after is not None else None
    detail = {"target": target.name, "distance_before": before, "distance_after": after,
              "distance_closed": closed}
    if not res.ok:
        return fail(res.error or "move_towards failed", raw=raw, **detail)
    if closed is None or closed <= 0:
        return fail("move_towards reported success but no distance was closed", raw=raw, **detail)
    return ok(f"walked to {target.name}: {before:.0f} -> {after:.1f} yd (closed {closed:.0f})", raw=raw, **detail)


def _quest_in_log(env: Env, quest_id: int) -> dict | None:
    return next((q for q in env.world.build_quest_log() if q.get("quest_id") == quest_id), None)


def step_quest(env: Env) -> Outcome:
    why = _area_skip(env)
    if why:
        return skip(why)
    quest = kt.QUEST_GIVER
    giver = _find_units(env, [quest.name])
    if not giver:
        return skip(f"{quest.name} not in view within {kt.MAX_WALK_YD:.0f} yd")
    giver_obj = giver[0]
    d = _distance(env, giver_obj)
    if d is None:
        return skip(f"{quest.name} position unknown")
    if d > npc.INTERACT_RANGE_YD:
        return skip(f"{quest.name} is {d:.0f} yd away, not within interact range "
                    f"({npc.INTERACT_RANGE_YD:.0f} yd); the probe does not walk for this step")
    existing = _quest_in_log(env, quest.quest_id)
    if existing is not None:
        return skip(f"quest {quest.quest_id} is already in the quest log ({existing.get('state_name')})")

    raws = []
    res = env.act("interact", guid=giver_obj.guid)
    raws.append({"interact": raw_result(res)})
    if not res.ok:
        return fail(res.error or "interact failed", raw=raws)

    def window_open():
        w = env.world.get_ui_state()
        return w if w and w.get("npc_guid") == giver_obj.guid and w.get("kind") in (
            "gossip", "quest_list", "quest_details") else None

    window = env.wait_until(window_open, env.timeouts.window_s)
    if window is None:
        return fail(f"no gossip/quest window from {quest.name} within {env.timeouts.window_s:.0f}s", raw=raws)
    try:
        offered = [q.get("quest_id") for q in window.get("quests", [])]
        if window.get("kind") != "quest_details" and quest.quest_id not in offered:
            return skip(f"quest {quest.quest_id} is not offered by {quest.name} "
                        f"(offered: {offered}); already done?")
        res = env.act("accept_quest", npc_guid=giver_obj.guid, quest_id=quest.quest_id)
        raws.append({"accept_quest": raw_result(res)})
        if not res.ok:
            return fail(res.error or "accept_quest failed", raw=raws)
        # accept_quest only sends the packet; the quest log is the confirmation.
        entry = env.wait_until(lambda: _quest_in_log(env, quest.quest_id), env.timeouts.quest_s)
    finally:
        env.act("close_window")
    if entry is None:
        return fail(f"accept_quest sent but quest {quest.quest_id} is not in the quest log "
                    f"after {env.timeouts.quest_s:.0f}s", raw=raws)
    return ok(f"accepted quest {quest.quest_id} ({quest.quest_title}) from {quest.name}", raw=raws,
              quest_id=quest.quest_id, state=entry.get("state_name"))


def step_combat(env: Env) -> Outcome:
    why = _area_skip(env)
    if why:
        return skip(why)
    me = env.world.get_my_object()
    if me is not None and me.unit_flags and (me.unit_flags & ac.UNIT_FLAG_IN_COMBAT):
        return skip("character is already in combat")
    pct = _health_pct(me)
    if pct is not None and pct < MIN_FIGHT_HEALTH_PCT:
        return skip(f"health is {pct:.0f}%, below {MIN_FIGHT_HEALTH_PCT}%; rest first")
    mobs = _find_units(env, kt.MOB_NAMES, level=kt.MOB_LEVEL, alive=True)
    if not mobs:
        return skip(f"no living level-{kt.MOB_LEVEL} {' / '.join(kt.MOB_NAMES)} in view within "
                    f"{kt.MAX_WALK_YD:.0f} yd")
    mob = mobs[0]
    start_d = _distance(env, mob)
    raws = []
    if start_d is not None and start_d > ac.MELEE_RANGE_YD - 1.0:
        res = env.act("move_towards", guid=mob.guid, stop_distance=MOVE_STOP_YD)
        raws.append({"move_towards": raw_result(res)})
        if not res.ok:
            return fail(f"could not reach {mob.name}: {res.error}", raw=raws, target=mob.name)
    res = env.act("set_target", guid=mob.guid)
    raws.append({"set_target": raw_result(res)})
    if not res.ok:
        return fail(res.error or "set_target failed", raw=raws, target=mob.name)
    t0 = env.clock()
    res = env.act("auto_attack", guid=mob.guid)
    raws.append({"auto_attack": raw_result(res)})
    if not res.ok:
        return fail(res.error or "auto_attack failed", raw=raws, target=mob.name)

    my_guid = env.session.player_guid
    health_start = env.world.get_object(mob.guid).health

    def swings():
        return [e for e in _events_since(env, t0, "attacker_state_update")
                if e.get("attacker_guid") == my_guid and e.get("victim_guid") == mob.guid]

    def target_dead():
        o = env.world.get_object(mob.guid)
        if o is not None and o.health == 0:
            return True
        return any(e.get("victim_guid") == mob.guid and e.get("now_dead")
                   for e in _events_since(env, t0, "attack_stop"))

    aborted = None
    deadline = env.clock() + env.timeouts.fight_s
    while not target_dead():
        pct = _health_pct(env.world.get_my_object())
        if pct is not None and pct < ABORT_FIGHT_HEALTH_PCT:
            aborted = f"own health fell to {pct:.0f}%"
            break
        if env.clock() >= deadline:
            aborted = f"mob still alive after {env.timeouts.fight_s:.0f}s"
            break
        env.sleep(env.timeouts.poll_s)
    died = bool(target_dead())
    if not died:
        env.act("stop_attack")

    hits = swings()
    damage = sum(int(e.get("damage") or 0) for e in hits)
    health_end = (env.world.get_object(mob.guid).health if env.world.get_object(mob.guid) else None)
    detail = {"target": mob.name, "target_guid": mob.guid, "swings": len(hits), "damage_dealt": damage,
              "target_died": died, "target_health_start": health_start, "target_health_end": health_end,
              "aborted": aborted}
    if died:
        env.shared["killed_guid"] = mob.guid
    if damage <= 0:
        return fail("no damage dealt" + (f" ({aborted})" if aborted else ""), raw=raws, **detail)
    state = "killed it" if died else f"left it alive ({aborted})"
    return ok(f"{mob.name}: {damage} damage in {len(hits)} swings, {state}", raw=raws, **detail)


def step_loot(env: Env) -> Outcome:
    killed = env.shared.get("killed_guid")

    def lootable():
        corpses = [o for o in env.world.get_objects().values()
                   if o.object_type == "unit" and o.is_lootable()
                   and (_distance(env, o) or 1e9) <= LOOT_WALK_YD]
        corpses.sort(key=lambda o: (o.guid != killed, _distance(env, o) or 1e9))
        return corpses[0] if corpses else None

    corpse = lootable() if not killed else env.wait_until(lootable, env.timeouts.corpse_s)
    if corpse is None:
        return skip("no lootable corpse within %.0f yd" % LOOT_WALK_YD)
    raws = []
    d = _distance(env, corpse)
    if d is not None and d > ac.LOOT_RANGE_YD - 1.0:
        res = env.act("move_towards", guid=corpse.guid, stop_distance=MOVE_STOP_YD)
        raws.append({"move_towards": raw_result(res)})
        if not res.ok:
            return fail(f"could not reach the corpse: {res.error}", raw=raws)
    res = env.act("loot", guid=corpse.guid)
    raws.append({"loot": raw_result(res)})
    detail = {"corpse": corpse.name, "coins": res.detail.get("coins"), "items": res.detail.get("items")}
    if not res.ok:
        return fail(res.error or "loot failed", raw=raws, **detail)
    n_items = len(res.detail.get("items") or [])
    return ok(f"looted {corpse.name}: {res.detail.get('coins', 0)} copper, {n_items} item(s)", raw=raws, **detail)


def _vitals(env: Env) -> dict:
    me = env.world.get_my_object()
    return {"health": getattr(me, "health", None), "max_health": getattr(me, "max_health", None),
            "health_pct": _health_pct(me), "mana_pct": _power_pct(me)}


def step_rest(env: Env) -> Outcome:
    before = _vitals(env)
    hp = before["health_pct"]
    if hp is None:
        return skip("own health unknown")
    # The rest reflex stops itself at REST_STOP_HP_PCT health, so health is what
    # is measured; mana is reported alongside.
    if hp >= restmod.REST_STOP_HP_PCT * 100:
        return skip(f"already at {hp:.0f}% health; nothing to regain")
    res = env.act("rest")
    raw = raw_result(res)
    if not res.ok:
        return fail(res.error or "rest failed", raw=raw, before=before)
    reflex = restmod.get_rest_reflex(env.session)
    try:
        def recovered():
            # No reflex thread runs here, so drive the reflex's own tick: it stops
            # itself on "recovered" or "aggro".
            reflex.tick(env.session, env.world)
            v = _vitals(env)
            health_ok = v["health_pct"] is not None and v["health_pct"] >= restmod.REST_STOP_HP_PCT * 100
            return health_ok or not reflex.active
        env.wait_until(recovered, env.timeouts.rest_s)
        after = _vitals(env)
    finally:
        reflex.stop(env.session, reason="probe_done")
    gained = (after["health"] - before["health"]) if after["health"] is not None and before["health"] is not None else None
    detail = {"before": before, "after": after, "health_regained": gained, "method": raw["detail"].get("method")}
    health_ok = after["health_pct"] is not None and after["health_pct"] >= restmod.REST_STOP_HP_PCT * 100
    if not health_ok or not gained or gained <= 0:
        return fail(f"rest did not recover health within {env.timeouts.rest_s:.0f}s "
                    f"({hp:.0f}% -> {after['health_pct'] if after['health_pct'] is not None else '?'}%)",
                    raw=raw, **detail)
    return ok(f"rested ({detail['method']}): health {hp:.0f}% -> {after['health_pct']:.0f}% (+{gained})",
              raw=raw, **detail)


STEPS = {"chat": step_chat, "move": step_move, "quest": step_quest, "combat": step_combat,
         "loot": step_loot, "rest": step_rest}
assert tuple(STEPS) == STEP_NAMES


# ── Runner and report ────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _step_record(name: str, outcome: Outcome, duration: float) -> dict:
    return {"name": name, "status": outcome.status, "duration_s": round(duration, 2),
            "summary": outcome.summary, "reason": outcome.reason, "error": outcome.error,
            "detail": outcome.detail, "raw": outcome.raw}


def run_step(name: str, fn, env: Env) -> dict:
    t0 = env.clock()
    try:
        outcome = fn(env)
    except Exception as e:  # one broken step must not hide the others
        outcome = fail(f"{type(e).__name__}: {e}")
    return _step_record(name, outcome, env.clock() - t0)


def build_report(*, character: str, host: str, started_at: float, duration: float, steps: list) -> dict:
    summary = {s: sum(1 for st in steps if st["status"] == s) for s in ("ok", "fail", "skipped")}
    return {
        "schema": REPORT_SCHEMA,
        "tool": "agent.tools.probe",
        "manual_only": True,
        "counts_as_human_intervention": True,
        "character": character,
        "host": host,
        "started_at": _iso(started_at),
        "duration_s": round(duration, 2),
        "overall": "fail" if summary["fail"] else "ok",
        "summary": summary,
        "steps": steps,
    }


def run_probe(connect, *, character: str, host: str, skip_steps=(), chat=None,
              line_id: str = lines.PROBE_LINE_ID, timeouts: Timeouts | None = None,
              settle_s: float = 8.0, actions: dict | None = None,
              clock=time.monotonic, sleep=time.sleep, wall=time.time) -> dict:
    """Log in with connect() -> session, run every step not in `skip_steps`, log
    out, return the report. A failed login skips the rest. The session is always
    logged out, and the chat-feed watcher stopped, even if a step blows up."""
    timeouts = timeouts or Timeouts()
    started, t_start = wall(), clock()
    records = []
    session = None
    login_t0 = clock()
    try:
        session = connect()
        sleep(settle_s)  # let perception and name queries settle
        world = session.world_state
        me = world.get_my_object()
        name = (me.name if me is not None and me.name else "") or character
        pos = session.player_position
        login = ok(f"{name} online" + (f", map {pos[0]} ({pos[1]:.0f}, {pos[2]:.0f})" if pos else ""),
                   character=name, position=list(pos) if pos else None,
                   level=getattr(me, "level", None), health=getattr(me, "health", None),
                   max_health=getattr(me, "max_health", None), objects_tracked=len(world.get_objects()))
    except Exception as e:
        login = fail(f"login failed: {type(e).__name__}: {e}")
    records.append(_step_record("login", login, clock() - login_t0))

    try:
        if login.status != "ok":
            for step in STEP_NAMES:
                records.append(_step_record(step, skip("login failed"), 0.0))
        else:
            env = Env(session=session, world=session.world_state, actions=actions or default_actions(),
                      character=login.detail["character"], chat=chat, line_id=line_id,
                      timeouts=timeouts, clock=clock, sleep=sleep)
            for step in STEP_NAMES:
                if step in skip_steps:
                    records.append(_step_record(step, skip("left out with --skip"), 0.0))
                else:
                    records.append(run_step(step, STEPS[step], env))
    finally:
        if chat is not None:
            chat.stop()
        if session is not None:
            try:
                session.logout()
            except Exception:
                logging.getLogger("agent.probe").exception("logout failed")
    return build_report(character=login.detail.get("character", character), host=host,
                        started_at=started, duration=clock() - t_start, steps=records)


def format_text(report: dict) -> str:
    """The compact per-step report."""
    s = report["summary"]
    out = [f"probe {report['character']} @ {report['host']}  {report['started_at']}  "
           f"{report['overall'].upper()}  (ok {s['ok']}, fail {s['fail']}, skipped {s['skipped']})  "
           f"{report['duration_s']:.0f}s"]
    for st in report["steps"]:
        out.append(f"  {st['name']:<7} {st['status']:<7} {st['duration_s']:>6.1f}s  {st['summary']}")
    return "\n".join(out)


def save_report(report: dict, path: str):
    """Write the JSON report atomically, so a reader never sees half a file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, default=repr)
        f.write("\n")
    os.replace(tmp, path)


def load_last_report(path: str) -> dict:
    """The last saved report, or {"status": "never_probed"} when there is none
    (or it is unreadable), which is what a panel should show for that agent."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        return {"status": "never_probed"}
    if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA:
        return {"status": "never_probed"}
    return report


# ── CLI ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m agent.tools.probe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.fill(MANUAL_ONLY_NOTICE, 78) + "\n\n" + textwrap.fill(
            "Needs WOW_ACCOUNT, WOW_PASSWORD and WOW_CHARACTER in the environment (see "
            ".claude/skills/live-agent-test). CHAT_FEED_TOKEN is sent to the chat-feed if set.", 78))
    p.add_argument("--confirm-manual-run", action="store_true",
                   help="required: you are a human, this is not a counted run, and you accept that the "
                        "probe counts as a human intervention")
    p.add_argument("--skip", default="", metavar="STEPS",
                   help="comma-separated steps to leave out (%s)" % ", ".join(STEP_NAMES))
    p.add_argument("--line", default=lines.PROBE_LINE_ID, metavar="ID",
                   help="standard chat line id for the chat step (default: %(default)s); see agent/lines.py")
    p.add_argument("--chat-feed", default="", metavar="URL",
                   help="chat-feed base URL (default: http://<WOW_HOST>:9500)")
    p.add_argument("--json", action="store_true", help="print the full JSON report instead of the text one")
    p.add_argument("--out", default="", metavar="FILE", help="also write the JSON report to FILE")
    p.add_argument("--settle", type=float, default=8.0, metavar="S",
                   help="seconds to wait after login for perception (default: %(default)s)")
    return p


def main(argv=None, *, connect=None) -> int:
    """`connect` is a test seam: a callable returning a logged-in session."""
    args = build_parser().parse_args(argv)
    if not args.confirm_manual_run:
        print(MANUAL_ONLY_NOTICE + "\nRe-run with --confirm-manual-run if that is what you want.",
              file=sys.stderr)
        return 2
    skipped = [s.strip() for s in args.skip.split(",") if s.strip()]
    unknown = [s for s in skipped if s not in STEP_NAMES]
    if unknown:
        print(f"unknown step(s) for --skip: {', '.join(unknown)}; valid: {', '.join(STEP_NAMES)}", file=sys.stderr)
        return 2
    if lines.line_error(args.line) is not None:
        print(lines.line_error(args.line), file=sys.stderr)
        return 2

    from ..config import load_config
    cfg = load_config()
    problems = cfg.validate()
    if problems:
        for pr in problems:
            print(f"CONFIG ERROR: {pr}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if connect is None:
        from .. import __main__ as agent_main
        log = logging.getLogger("agent.probe")

        def connect():
            return agent_main._connect_and_login(cfg, log)

    chat = None
    if "chat" not in skipped:
        chat = ChatFeedWatcher(args.chat_feed or f"http://{cfg.wow_host}:9500",
                               token=cfg.chat_feed_token)

    report = run_probe(connect, character=cfg.character or f"guid:{cfg.char_guid}", host=cfg.wow_host,
                       skip_steps=skipped, chat=chat, line_id=args.line, settle_s=args.settle)
    if args.out:
        save_report(report, args.out)
    print(json.dumps(report, indent=2, sort_keys=True, default=repr) if args.json else format_text(report))
    return 1 if report["overall"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
