"""Unit tests for agent.tools.probe (#139). Fakes only: no network, no server.

The probe's job is to report what the action layer and the chat-feed say, so
these tests script those answers and check the report and the safety rules
(skip instead of walking somewhere random, never trust the agent's own belief
for chat, manual-only guard)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from agent import actions as ac
from agent import known_targets as kt
from agent import perception as per
from agent import update_fields as uf
from agent.reflexes import rest as restmod
from agent.tools import probe
from agent.tests import builders

ME = 0xF130000000000001
ERONA = 0xF130000000000010
MOB = 0xF130000000000020
MOB2 = 0xF130000000000021
MAP = kt.START_AREA_MAP_ID


class Clock:
    """Fake time: sleep() advances it and runs the registered hooks, which is how
    a test makes the 'server' answer while the probe waits."""

    def __init__(self):
        self.t = 0.0
        self.hooks = []

    def clock(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        for hook in list(self.hooks):
            hook(self.t)


class FakeAction:
    def __init__(self, fn=None):
        self.fn = fn or (lambda session, world, **p: ac.ActionResult(ok=True))
        self.calls = []

    def run(self, session, world, **params):
        self.calls.append(params)
        return self.fn(session, world, **params)


def unit(guid, name, x, y, level=1, health=10, max_health=10, **kw):
    return per.ObjectInfo(guid=guid, object_type="unit", name=name, level=level, health=health,
                          max_health=max_health, position=(MAP, x, y, 0.0, 0.0), **kw)


def make_env(units=(), position=(MAP, 0.0, 0.0, 0.0, 0.0), health=100, max_health=100, **env_kw):
    world = per.WorldState()
    world.my_guid = ME
    me = per.ObjectInfo(guid=ME, object_type="player", name="Tester", level=1, health=health,
                        max_health=max_health, position=position)
    world.objects[ME] = me
    for u in units:
        world.objects[u.guid] = u
    session = builders.FakeSession(player_position=position, player_guid=ME, events=[], world_state=world,
                              sent=[], _send_packet=lambda op, payload=b"": None, race=10)
    clk = Clock()
    env_kw.setdefault("actions", {})
    env = probe.Env(session=session, world=world, character="Tester", clock=clk.clock, sleep=clk.sleep,
                    **env_kw)
    env.clk = clk
    return env


def erona(x=20.0, y=0.0):
    return unit(ERONA, kt.QUEST_GIVER.name, x, y, level=5)


# ── Chat ─────────────────────────────────────────────────────────────────

class FakeChat:
    def __init__(self, hears=True, start_error=None):
        self.hears, self.start_error = hears, start_error
        self.started = self.stopped = False
        self.waited = None

    def start(self):
        if self.start_error:
            raise probe.ChatFeedError(self.start_error)
        self.started = True

    def mark(self):
        return 0

    def wait_for(self, sender, text, kind="say", timeout=10.0):
        self.waited = (sender, text, kind)
        return {"kind": kind, "sender": sender, "text": text} if self.hears else None

    def stop(self):
        self.stopped = True


class ChatStepTest(unittest.TestCase):
    def test_ok_when_the_feed_hears_the_standard_line(self):
        chat = FakeChat()
        say = FakeAction()
        env = make_env(actions={"say": say}, chat=chat)
        out = probe.step_chat(env)
        self.assertEqual(out.status, "ok", out)
        self.assertEqual(say.calls, [{"message": "Still here."}])
        self.assertEqual(chat.waited, ("Tester", "Still here.", "say"))

    def test_fail_when_the_agent_says_it_sent_but_the_feed_never_hears_it(self):
        env = make_env(actions={"say": FakeAction()}, chat=FakeChat(hears=False))
        out = probe.step_chat(env)
        self.assertEqual(out.status, "fail")
        self.assertIn("not seen in the chat-feed", out.error)

    def test_fail_not_ok_when_the_feed_is_unreachable(self):
        say = FakeAction()
        env = make_env(actions={"say": say}, chat=FakeChat(start_error="cannot open http://x"))
        out = probe.step_chat(env)
        self.assertEqual(out.status, "fail")
        self.assertIn("unconfirmed", out.error)
        self.assertEqual(say.calls, [], "must not talk when it cannot be confirmed")

    def test_fail_without_a_feed(self):
        out = probe.step_chat(make_env(actions={"say": FakeAction()}, chat=None))
        self.assertEqual(out.status, "fail")

    def test_fail_when_say_itself_fails(self):
        say = FakeAction(lambda s, w, **p: ac.ActionResult(ok=False, error="boom"))
        out = probe.step_chat(make_env(actions={"say": say}, chat=FakeChat()))
        self.assertEqual((out.status, out.error), ("fail", "boom"))

    def test_a_line_that_is_not_standard_never_reaches_say(self):
        say = FakeAction()
        out = probe.step_chat(make_env(actions={"say": say}, chat=FakeChat(), line_id="}"))
        self.assertEqual(out.status, "fail")
        self.assertEqual(say.calls, [])

    def test_real_say_action_sends_exactly_the_standard_line(self):
        sent = []
        env = make_env(actions=probe.default_actions(), chat=FakeChat())
        env.session._send_packet = lambda op, payload=b"": sent.append((op, payload))
        self.assertEqual(probe.step_chat(env).status, "ok")
        (opcode, payload), = sent
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)
        self.assertTrue(payload.endswith(b"Still here.\x00"))


# ── Chat-feed oracle ─────────────────────────────────────────────────────

def frame(event):
    return [b"id: 1\n", b"event: chat\n", ("data: %s\n" % json.dumps(event)).encode(), b"\n"]


class FakeResponse:
    def __init__(self, lines_):
        self._lines = lines_
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class SseTest(unittest.TestCase):
    def test_parse_frames_keepalives_and_multiline_data(self):
        raw = [b": keepalive\n", b"\n", b"id: 5\n", b"event: chat\n", b"data: {\"a\":\n", b"data: 1}\n", b"\n",
               b"data: plain\n", b"\n"]
        self.assertEqual(list(probe.parse_sse(raw)),
                         [("chat", "{\"a\":\n1}"), ("message", "plain")])


class ChatFeedWatcherTest(unittest.TestCase):
    def watcher(self, frames, **kw):
        requests = []

        def opener(req, timeout=None):
            requests.append(req)
            return FakeResponse(frames)
        clk = Clock()
        w = probe.ChatFeedWatcher("http://feed.test:9500/", opener=opener, sleep=clk.sleep, clock=clk.clock, **kw)
        w.requests = requests
        w.clk = clk
        return w

    def heard(self, **over):
        event = {"kind": "say", "sender": "Tester", "channel": None, "text": "Still here."}
        event.update(over)
        return event

    def test_only_events_after_the_mark_count(self):
        # The stream replays history on connect; an old identical line must not confirm a new say.
        w = self.watcher(frame(self.heard()))
        w.start()
        w._thread.join(2)
        self.assertEqual(len(w.events), 1)
        w.mark()
        self.assertIsNone(w.wait_for("Tester", "Still here.", timeout=1.0))
        w.events.append(self.heard())
        self.assertIsNotNone(w.wait_for("tester", "Still here.", timeout=1.0))

    def test_sender_text_and_kind_must_all_match(self):
        w = self.watcher([])
        w.start()
        w.mark()
        w.events.extend([self.heard(sender="Someone"), self.heard(text="Still here"),
                         self.heard(kind="yell")])
        self.assertIsNone(w.wait_for("Tester", "Still here.", timeout=1.0))

    def test_connects_to_the_stream_url_with_the_token_header(self):
        w = self.watcher([], token="fake-token")
        w.start()
        (req,) = w.requests
        self.assertEqual(req.full_url, "http://feed.test:9500/api/chat/stream")
        self.assertEqual(req.get_header("Authorization"), "Bearer fake-token")

    def test_unreachable_feed_raises_without_leaking_the_token(self):
        def opener(req, timeout=None):
            raise OSError("connection refused")
        w = probe.ChatFeedWatcher("http://feed.test:9500", token="fake-token", opener=opener, sleep=lambda s: None)
        with self.assertRaises(probe.ChatFeedError) as cm:
            w.start()
        self.assertNotIn("fake-token", str(cm.exception))

    def test_non_chat_and_garbage_frames_are_ignored(self):
        w = self.watcher([b"event: ping\n", b"data: {}\n", b"\n", b"event: chat\n", b"data: nope\n", b"\n"])
        w.start()
        w._thread.join(2)
        self.assertEqual(w.events, [])

    def test_stop_closes_the_stream(self):
        w = self.watcher([])
        w.start()
        resp = w._resp
        w.stop()
        self.assertTrue(resp.closed)


# ── Move ─────────────────────────────────────────────────────────────────

def walker(session_pos_after):
    def fn(session, world, guid, stop_distance):
        session.player_position = session_pos_after
        return ac.ActionResult(ok=True, detail={"position": session_pos_after[1:4]})
    return FakeAction(fn)


class MoveStepTest(unittest.TestCase):
    def test_ok_reports_the_distance_closed(self):
        mover = walker((MAP, 17.0, 0.0, 0.0, 0.0))
        env = make_env([erona()], actions={"move_towards": mover})
        out = probe.step_move(env)
        self.assertEqual(out.status, "ok", out)
        self.assertEqual(mover.calls, [{"guid": ERONA, "stop_distance": probe.MOVE_STOP_YD}])
        self.assertAlmostEqual(out.detail["distance_before"], 20.0)
        self.assertAlmostEqual(out.detail["distance_after"], 3.0)
        self.assertAlmostEqual(out.detail["distance_closed"], 17.0)
        self.assertEqual(out.raw["ok"], True)

    def test_skipped_when_not_on_the_start_area_map(self):
        mover = FakeAction()
        env = make_env([erona()], position=(0, 0.0, 0.0, 0.0, 0.0), actions={"move_towards": mover})
        out = probe.step_move(env)
        self.assertEqual(out.status, "skipped")
        self.assertIn("map 0", out.reason)
        self.assertEqual(mover.calls, [])

    def test_skipped_when_the_npc_is_not_perceived(self):
        mover = FakeAction()
        out = probe.step_move(make_env([], actions={"move_towards": mover}))
        self.assertEqual(out.status, "skipped")
        self.assertEqual(mover.calls, [])

    def test_skipped_when_the_npc_is_too_far_to_vouch_for(self):
        mover = FakeAction()
        env = make_env([erona(x=kt.MAX_WALK_YD + 50)], actions={"move_towards": mover})
        self.assertEqual(probe.step_move(env).status, "skipped")
        self.assertEqual(mover.calls, [])

    def test_a_namesake_with_a_different_name_is_not_a_fixture(self):
        mover = FakeAction()
        env = make_env([unit(ERONA, "Magistrix Erona's Cousin", 10, 0)], actions={"move_towards": mover})
        self.assertEqual(probe.step_move(env).status, "skipped")

    def test_skipped_when_already_next_to_the_npc(self):
        mover = FakeAction()
        env = make_env([erona(x=2.0)], actions={"move_towards": mover})
        out = probe.step_move(env)
        self.assertEqual(out.status, "skipped")
        self.assertIn("already", out.reason)

    def test_fail_when_the_action_fails(self):
        mover = FakeAction(lambda s, w, **p: ac.ActionResult(ok=False, error="stuck"))
        out = probe.step_move(make_env([erona()], actions={"move_towards": mover}))
        self.assertEqual((out.status, out.error), ("fail", "stuck"))

    def test_fail_when_it_claims_success_but_closed_nothing(self):
        out = probe.step_move(make_env([erona()], actions={"move_towards": FakeAction()}))
        self.assertEqual(out.status, "fail")
        self.assertIn("no distance was closed", out.error)


# ── Quest ────────────────────────────────────────────────────────────────

def put_in_quest_log(world, quest_id):
    world.objects[ME].raw_fields[uf.PLAYER_QUEST_LOG_1_1] = quest_id
    world.objects[ME].raw_fields[uf.PLAYER_QUEST_LOG_1_1 + 1] = 3


def quest_env(accept_effect=True, window=True, offered=(8325,), npc_distance=2.0, **kw):
    env = make_env([erona(x=npc_distance)], **kw)

    def interact(session, world, guid):
        if window:
            world.ui_state = {"kind": "gossip", "npc_guid": guid,
                              "quests": [{"quest_id": q} for q in offered]}
        return ac.ActionResult(ok=True, detail={"kind": "gossip_hello"})

    def accept(session, world, npc_guid, quest_id):
        if accept_effect:
            put_in_quest_log(world, quest_id)
        return ac.ActionResult(ok=True, detail={})

    env.actions = {"interact": FakeAction(interact), "accept_quest": FakeAction(accept),
                   "close_window": FakeAction()}
    return env


class QuestStepTest(unittest.TestCase):
    def test_ok_when_the_quest_lands_in_the_quest_log(self):
        env = quest_env()
        out = probe.step_quest(env)
        self.assertEqual(out.status, "ok", out)
        self.assertEqual(env.actions["accept_quest"].calls, [{"npc_guid": ERONA, "quest_id": 8325}])
        self.assertEqual(len(env.actions["close_window"].calls), 1)

    def test_fail_when_accept_is_sent_but_the_log_never_shows_it(self):
        out = probe.step_quest(quest_env(accept_effect=False))
        self.assertEqual(out.status, "fail")
        self.assertIn("not in the quest log", out.error)

    def test_fail_when_no_window_opens(self):
        env = quest_env(window=False)
        out = probe.step_quest(env)
        self.assertEqual(out.status, "fail")
        self.assertEqual(env.actions["accept_quest"].calls, [])

    def test_skipped_when_the_quest_is_already_in_the_log(self):
        env = quest_env()
        put_in_quest_log(env.world, 8325)
        out = probe.step_quest(env)
        self.assertEqual(out.status, "skipped")
        self.assertEqual(env.actions["interact"].calls, [])

    def test_skipped_when_the_quest_is_not_offered(self):
        env = quest_env(offered=())
        out = probe.step_quest(env)
        self.assertEqual(out.status, "skipped")
        self.assertIn("not offered", out.reason)
        self.assertEqual(env.actions["accept_quest"].calls, [])

    def test_skipped_out_of_range_without_walking(self):
        env = quest_env(npc_distance=30.0)
        env.actions["move_towards"] = FakeAction()
        out = probe.step_quest(env)
        self.assertEqual(out.status, "skipped")
        self.assertEqual(env.actions["move_towards"].calls, [])
        self.assertEqual(env.actions["interact"].calls, [])

    def test_skipped_without_the_npc(self):
        env = make_env([])
        self.assertEqual(probe.step_quest(env).status, "skipped")


# ── Combat ───────────────────────────────────────────────────────────────

def combat_env(damage=4, kills=True, mob_x=30.0, **kw):
    mob = unit(MOB, "Mana Wyrm", mob_x, 0.0, level=1, health=10, max_health=10)
    env = make_env([mob], **kw)

    def move(session, world, guid, stop_distance):
        session.player_position = (MAP, mob_x - 3.0, 0.0, 0.0, 0.0)
        return ac.ActionResult(ok=True)

    def attack(session, world, guid):
        t_attack = env.clk.t

        def hook(now):
            if now > t_attack and not getattr(hook, "done", False):
                hook.done = True
                if damage:
                    session.events.append({"kind": "attacker_state_update", "t": now, "attacker_guid": ME,
                                           "victim_guid": MOB, "damage": damage})
                if kills:
                    world.objects[MOB].health = 0
                    session.events.append({"kind": "attack_stop", "t": now, "attacker_guid": ME,
                                           "victim_guid": MOB, "now_dead": True})
        env.clk.hooks.append(hook)
        return ac.ActionResult(ok=True, detail={"guid": guid})

    env.actions = {"move_towards": FakeAction(move), "set_target": FakeAction(), "auto_attack": FakeAction(attack),
                   "stop_attack": FakeAction()}
    return env


class CombatStepTest(unittest.TestCase):
    def test_ok_reports_damage_and_the_kill(self):
        env = combat_env(damage=4)
        out = probe.step_combat(env)
        self.assertEqual(out.status, "ok", out)
        self.assertEqual(out.detail["damage_dealt"], 4)
        self.assertEqual(out.detail["swings"], 1)
        self.assertTrue(out.detail["target_died"])
        self.assertEqual(env.shared["killed_guid"], MOB)
        self.assertEqual(env.actions["stop_attack"].calls, [])
        names = [list(r)[0] for r in out.raw]
        self.assertEqual(names, ["move_towards", "set_target", "auto_attack"])

    def test_damage_only_counts_our_swings_at_that_mob(self):
        env = combat_env(damage=4)
        env.session.events.append({"kind": "attacker_state_update", "t": 99.0, "attacker_guid": MOB,
                                   "victim_guid": ME, "damage": 50})
        self.assertEqual(probe.step_combat(env).detail["damage_dealt"], 4)

    def test_fail_when_no_damage_is_dealt(self):
        env = combat_env(damage=0, kills=False)
        out = probe.step_combat(env)
        self.assertEqual(out.status, "fail")
        self.assertIn("no damage dealt", out.error)
        self.assertEqual(len(env.actions["stop_attack"].calls), 1, "must stop attacking on timeout")

    def test_alive_after_the_timeout_is_ok_if_it_hurt_the_mob_and_stops_attacking(self):
        env = combat_env(damage=2, kills=False)
        out = probe.step_combat(env)
        self.assertEqual(out.status, "ok")
        self.assertFalse(out.detail["target_died"])
        self.assertIn("after", out.detail["aborted"])
        self.assertEqual(len(env.actions["stop_attack"].calls), 1)
        self.assertNotIn("killed_guid", env.shared)

    def test_aborts_when_our_own_health_collapses(self):
        env = combat_env(damage=2, kills=False)
        env.clk.hooks.append(lambda now: setattr(env.world.objects[ME], "health", 10))
        out = probe.step_combat(env)
        self.assertIn("own health", out.detail["aborted"])
        self.assertLess(env.clk.t, probe.Timeouts().fight_s)

    def test_fail_when_the_mob_cannot_be_reached(self):
        env = combat_env()
        env.actions["move_towards"] = FakeAction(lambda s, w, **p: ac.ActionResult(ok=False, error="stuck"))
        out = probe.step_combat(env)
        self.assertEqual(out.status, "fail")
        self.assertEqual(env.actions["auto_attack"].calls, [])

    def test_fail_when_auto_attack_is_refused(self):
        env = combat_env()
        env.actions["auto_attack"] = FakeAction(lambda s, w, **p: ac.ActionResult(ok=False, error="not hostile"))
        self.assertEqual(probe.step_combat(env).error, "not hostile")

    def test_skipped_without_a_level_one_mob_in_view(self):
        for units in ([], [unit(MOB, "Mana Wyrm", 30, 0, level=3)], [unit(MOB, "Mana Wyrm", 30, 0, health=0)],
                      [unit(MOB, "Wolf", 30, 0)], [unit(MOB, "Mana Wyrm", 500, 0)]):
            with self.subTest(units=units):
                env = make_env(units, actions={"auto_attack": FakeAction()})
                out = probe.step_combat(env)
                self.assertEqual(out.status, "skipped")
                self.assertEqual(env.actions["auto_attack"].calls, [])

    def test_picks_the_nearest_mob(self):
        env = combat_env()
        env.world.objects[MOB2] = unit(MOB2, "Springpaw Cub", 10.0, 0.0)
        probe.step_combat(env)
        self.assertEqual(env.actions["move_towards"].calls[0]["guid"], MOB2)

    def test_skipped_when_hurt_or_already_fighting(self):
        env = combat_env(health=40, max_health=100)
        self.assertEqual(probe.step_combat(env).status, "skipped")
        env = combat_env()
        env.world.objects[ME].unit_flags = ac.UNIT_FLAG_IN_COMBAT
        self.assertEqual(probe.step_combat(env).status, "skipped")

    def test_skipped_off_the_start_area_map(self):
        env = combat_env(position=(1, 0.0, 0.0, 0.0, 0.0))
        self.assertEqual(probe.step_combat(env).status, "skipped")


# ── Loot ─────────────────────────────────────────────────────────────────

def corpse(guid=MOB, x=2.0):
    return unit(guid, "Mana Wyrm", x, 0.0, health=0, dynamic_flags=per.UNIT_DYNFLAG_LOOTABLE)


class LootStepTest(unittest.TestCase):
    def looter(self, **detail):
        return FakeAction(lambda s, w, guid: ac.ActionResult(
            ok=True, detail={"guid": guid, "coins": 7, "items": [{"slot": 0}], **detail}))

    def test_skipped_when_there_is_no_corpse(self):
        loot = self.looter()
        out = probe.step_loot(make_env([], actions={"loot": loot}))
        self.assertEqual(out.status, "skipped")
        self.assertEqual(loot.calls, [])

    def test_ok_loots_the_corpse_we_just_killed_in_range(self):
        loot = self.looter()
        env = make_env([corpse()], actions={"loot": loot, "move_towards": FakeAction()})
        env.shared["killed_guid"] = MOB
        out = probe.step_loot(env)
        self.assertEqual(out.status, "ok", out)
        self.assertEqual(loot.calls, [{"guid": MOB}])
        self.assertEqual(out.detail["coins"], 7)
        self.assertEqual(env.actions["move_towards"].calls, [])

    def test_walks_to_a_far_corpse_first(self):
        env = make_env([corpse(x=20.0)], actions={"loot": self.looter(), "move_towards": FakeAction()})
        self.assertEqual(probe.step_loot(env).status, "ok")
        self.assertEqual(len(env.actions["move_towards"].calls), 1)

    def test_a_corpse_beyond_the_walk_limit_is_not_chased(self):
        env = make_env([corpse(x=probe.LOOT_WALK_YD + 10)], actions={"loot": self.looter()})
        self.assertEqual(probe.step_loot(env).status, "skipped")

    def test_fail_when_loot_fails(self):
        loot = FakeAction(lambda s, w, guid: ac.ActionResult(ok=False, error="nothing there", detail={"guid": guid}))
        out = probe.step_loot(make_env([corpse()], actions={"loot": loot}))
        self.assertEqual((out.status, out.error), ("fail", "nothing there"))


# ── Rest ─────────────────────────────────────────────────────────────────

class RestStepTest(unittest.TestCase):
    def env(self, health, regen_per_s=5, **kw):
        env = make_env(health=health, max_health=100, **kw)
        env.actions = {"rest": ac.REGISTRY["rest"]}
        env.session.rest_reflex = None  # fresh reflex
        env.session.sent = []
        env.session._send_packet = lambda op, payload=b"": env.session.sent.append(op)

        def regen(now):
            reflex = env.session.rest_reflex
            me = env.world.objects[ME]
            if reflex is not None and reflex.active:
                me.health = min(me.max_health, me.health + regen_per_s * env.timeouts.poll_s)
        env.clk.hooks.append(regen)
        return env

    def test_ok_reports_health_regained_and_stands_back_up(self):
        env = self.env(health=40)
        out = probe.step_rest(env)
        self.assertEqual(out.status, "ok", out)
        self.assertGreater(out.detail["health_regained"], 0)
        self.assertGreaterEqual(out.detail["after"]["health_pct"], 90)
        self.assertFalse(restmod.get_rest_reflex(env.session).active)
        self.assertEqual(env.session.sent[-1], restmod.CMSG_STANDSTATECHANGE)

    def test_fail_when_nothing_regenerates_in_time(self):
        env = self.env(health=40, regen_per_s=0)
        env.timeouts = probe.Timeouts(rest_s=5.0)
        out = probe.step_rest(env)
        self.assertEqual(out.status, "fail")
        self.assertFalse(restmod.get_rest_reflex(env.session).active, "must not stay seated")

    def test_skipped_when_already_healthy(self):
        env = self.env(health=95)
        self.assertEqual(probe.step_rest(env).status, "skipped")

    def test_fail_when_rest_is_refused(self):
        env = self.env(health=40)
        env.world.objects[ME].unit_flags = restmod.UNIT_FLAG_IN_COMBAT
        out = probe.step_rest(env)
        self.assertEqual(out.status, "fail")
        self.assertIn("combat", out.error)


# ── Runner and report ────────────────────────────────────────────────────

class FakeSession:
    def __init__(self):
        self.world_state = per.WorldState()
        self.world_state.my_guid = ME
        self.world_state.objects[ME] = per.ObjectInfo(guid=ME, object_type="player", name="Tester", level=1,
                                                      health=50, max_health=50,
                                                      position=(MAP, 1.0, 2.0, 3.0, 0.0))
        self.player_position = (MAP, 1.0, 2.0, 3.0, 0.0)
        self.player_guid = ME
        self.events = []
        self.logged_out = False

    def logout(self):
        self.logged_out = True


def run(session=None, connect=None, steps=None, **kw):
    session = session or FakeSession()
    kw.setdefault("settle_s", 0.0)
    kw.setdefault("sleep", lambda s: None)
    patches = {name: (lambda env: probe.ok("fine")) for name in probe.STEP_NAMES}
    patches.update(steps or {})
    with mock.patch.dict(probe.STEPS, patches):
        report = probe.run_probe(connect or (lambda: session), character="Tester", host="host.test", **kw)
    return session, report


class RunnerTest(unittest.TestCase):
    def test_all_steps_run_in_order_and_the_report_is_valid_json(self):
        session, report = run()
        self.assertEqual([s["name"] for s in report["steps"]], ["login", *probe.STEP_NAMES])
        self.assertEqual(report["overall"], "ok")
        self.assertEqual(report["summary"], {"ok": 7, "fail": 0, "skipped": 0})
        self.assertEqual(report["character"], "Tester")
        self.assertTrue(report["manual_only"])
        self.assertTrue(report["counts_as_human_intervention"])
        self.assertEqual(report["schema"], probe.REPORT_SCHEMA)
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertTrue(session.logged_out)
        for step in report["steps"]:
            self.assertEqual(set(step), {"name", "status", "duration_s", "summary", "reason", "error", "detail", "raw"})

    def test_a_failed_step_fails_the_report_but_later_steps_still_run(self):
        _, report = run(steps={"move": lambda env: probe.fail("stuck"), "loot": lambda env: probe.skip("no corpse")})
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["summary"], {"ok": 5, "fail": 1, "skipped": 1})
        by_name = {s["name"]: s for s in report["steps"]}
        self.assertEqual(by_name["move"]["error"], "stuck")
        self.assertEqual(by_name["loot"]["reason"], "no corpse")
        self.assertEqual(by_name["rest"]["status"], "ok")

    def test_skipped_steps_do_not_fail_the_report(self):
        _, report = run(steps={n: (lambda env: probe.skip("nope")) for n in probe.STEP_NAMES})
        self.assertEqual(report["overall"], "ok")
        self.assertEqual(report["summary"]["skipped"], 6)

    def test_a_crashing_step_becomes_a_failure_not_a_traceback(self):
        def boom(env):
            raise KeyError("x")
        session, report = run(steps={"combat": boom})
        by_name = {s["name"]: s for s in report["steps"]}
        self.assertEqual(by_name["combat"]["status"], "fail")
        self.assertIn("KeyError", by_name["combat"]["error"])
        self.assertEqual(by_name["rest"]["status"], "ok")
        self.assertTrue(session.logged_out)

    def test_login_failure_skips_everything_else(self):
        def connect():
            raise RuntimeError("auth failed")
        _, report = run(connect=connect)
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["steps"][0]["status"], "fail")
        self.assertIn("auth failed", report["steps"][0]["error"])
        self.assertTrue(all(s["status"] == "skipped" and s["reason"] == "login failed" for s in report["steps"][1:]))

    def test_skip_option_leaves_steps_out(self):
        called = []
        _, report = run(skip_steps=("combat", "loot"),
                        steps={"combat": lambda env: called.append("combat") or probe.ok("x")})
        self.assertEqual(called, [])
        by_name = {s["name"]: s for s in report["steps"]}
        self.assertEqual(by_name["combat"]["status"], "skipped")
        self.assertEqual(by_name["loot"]["status"], "skipped")

    def test_chat_watcher_is_stopped_and_session_logged_out_even_on_error(self):
        chat = FakeChat()
        session, _ = run(chat=chat, steps={"chat": lambda env: 1 / 0})
        self.assertTrue(chat.stopped)
        self.assertTrue(session.logged_out)

    def test_login_detail_names_the_character_and_position(self):
        _, report = run()
        login = report["steps"][0]
        self.assertEqual(login["detail"]["character"], "Tester")
        self.assertEqual(login["detail"]["position"], [MAP, 1.0, 2.0, 3.0, 0.0])

    def test_report_never_contains_credentials_or_the_feed_token(self):
        with mock.patch.dict(os.environ, {"WOW_PASSWORD": "fake-secret", "CHAT_FEED_TOKEN": "fake-token"}):
            _, report = run()
        blob = json.dumps(report)
        self.assertNotIn("fake-secret", blob)
        self.assertNotIn("fake-token", blob)

    def test_text_report_is_compact(self):
        _, report = run(steps={"move": lambda env: probe.fail("stuck"), "loot": lambda env: probe.skip("no corpse")})
        text = probe.format_text(report)
        self.assertEqual(len(text.splitlines()), 1 + 7)
        self.assertIn("FAIL", text.splitlines()[0])
        self.assertIn("move", text)
        self.assertIn("stuck", text)
        self.assertIn("no corpse", text)


class ReportFileTest(unittest.TestCase):
    def test_roundtrip_and_never_probed(self):
        _, report = run()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "probe.json")
            self.assertEqual(probe.load_last_report(path), {"status": "never_probed"})
            probe.save_report(report, path)
            self.assertEqual(probe.load_last_report(path), json.loads(json.dumps(report)))
            self.assertEqual(os.listdir(d), ["probe.json"], "no temp file left behind")

    def test_garbage_or_foreign_files_read_as_never_probed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "probe.json")
            for content in ("not json", "[]", json.dumps({"schema": 999})):
                with open(path, "w") as f:
                    f.write(content)
                self.assertEqual(probe.load_last_report(path), {"status": "never_probed"})


# ── CLI ──────────────────────────────────────────────────────────────────

class CliTest(unittest.TestCase):
    ENV = {"WOW_ACCOUNT": "fakeacct", "WOW_PASSWORD": "fakepass", "WOW_CHARACTER": "Tester",
           "WOW_HOST": "host.test"}

    def invoke(self, argv, connect=None, steps=None):
        out, err = io.StringIO(), io.StringIO()
        patches = {name: (lambda env: probe.ok("fine")) for name in probe.STEP_NAMES}
        patches.update(steps or {})
        with mock.patch.dict(os.environ, self.ENV), mock.patch.dict(probe.STEPS, patches), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = probe.main(argv, connect=connect)
        return rc, out.getvalue(), err.getvalue()

    def test_help_states_the_intervention_rule(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            probe.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = " ".join(out.getvalue().split())
        self.assertIn("MANUAL ONLY", text)
        self.assertIn("human intervention", text)
        self.assertIn("docs/AGENT-RUN-1-10.md", text)
        self.assertIn("counted run", text)
        self.assertIn("never schedule it", text)

    def test_refuses_to_run_without_the_manual_confirmation(self):
        connected = []
        rc, out, err = self.invoke(["--skip", "chat"], connect=lambda: connected.append(1))
        self.assertEqual(rc, 2)
        self.assertEqual(connected, [], "must not log in")
        self.assertIn("MANUAL ONLY", err)
        self.assertEqual(out, "")

    def test_bad_arguments_exit_2_before_logging_in(self):
        connected = []
        for argv in (["--confirm-manual-run", "--skip", "fly"], ["--confirm-manual-run", "--line", "}"]):
            with self.subTest(argv=argv):
                rc, _, err = self.invoke(argv, connect=lambda: connected.append(1))
                self.assertEqual(rc, 2)
                self.assertTrue(err)
        self.assertEqual(connected, [])

    def test_missing_config_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"WOW_ACCOUNT": "", "WOW_PASSWORD": "", "WOW_CHARACTER": ""}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = probe.main(["--confirm-manual-run"], connect=lambda: self.fail("must not connect"))
        self.assertEqual(rc, 2)
        self.assertIn("CONFIG ERROR", err.getvalue())

    def test_runs_prints_text_and_exits_0(self):
        rc, out, _ = self.invoke(["--confirm-manual-run", "--skip", "chat", "--settle", "0"],
                                 connect=FakeSession)
        self.assertEqual(rc, 0)
        self.assertIn("OK", out.splitlines()[0])

    def test_exit_1_when_a_step_fails(self):
        rc, _, _ = self.invoke(["--confirm-manual-run", "--skip", "chat", "--settle", "0"], connect=FakeSession,
                               steps={"move": lambda env: probe.fail("stuck")})
        self.assertEqual(rc, 1)

    def test_json_flag_and_out_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "last.json")
            rc, out, _ = self.invoke(["--confirm-manual-run", "--skip", "chat", "--settle", "0", "--json",
                                      "--out", path], connect=FakeSession)
            self.assertEqual(rc, 0)
            printed = json.loads(out)
            self.assertEqual(printed["overall"], "ok")
            self.assertEqual(probe.load_last_report(path), printed)

    def test_chat_feed_defaults_to_the_realm_host(self):
        made = []

        class Recorder(FakeChat):
            def __init__(self, url, token=""):
                super().__init__()
                made.append((url, token))
        with mock.patch.object(probe, "ChatFeedWatcher", Recorder), \
                mock.patch.dict(os.environ, {"CHAT_FEED_TOKEN": "fake-token"}):
            self.invoke(["--confirm-manual-run", "--settle", "0"], connect=FakeSession)
        self.assertEqual(made, [("http://host.test:9500", "fake-token")])


if __name__ == "__main__":
    unittest.main()
