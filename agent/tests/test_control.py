"""Unit tests for agent.control (#178): the operator walk command and its HTTP endpoint.

A real WorldState and a real Mover (fake clock) drive the movement code; the packets go
to a recording `_send_packet`, so nothing here needs a game server. The tests also pin
the safety property the issue insists on: a walk only ever sends the character's own
MSG_MOVE_* packets."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent import actions as ac
from agent import control
from agent import movement as mv
from agent import perception as per
from agent import update_object as uo
from agent.http_api import AgentObserver, make_server

TOKEN = "control-token-for-tests"
ME, RUBENS, MOB = 0x10, 0x30, 0x40


def _create(ws, guid, kind, x, y, z, name=""):
    ws.update_object(uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=kind,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={}))
    if name:
        ws.get_object(guid).name = name


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def make_session(position=(530, 10.0, 20.0, 30.0, 0.0), real_sleep=None):
    ws = per.WorldState()
    ws.set_my_guid(ME)
    ws.set_my_map(position[0])
    _create(ws, ME, uo.TYPEID_PLAYER, *position[1:4])
    _create(ws, RUBENS, uo.TYPEID_PLAYER, 40.0, 20.0, 30.0, name="Rubens")
    _create(ws, MOB, uo.TYPEID_UNIT, 12.0, 20.0, 30.0, name="Wolf")
    ws.get_my_object().health = 100
    sent = []
    sess = SimpleNamespace(world_state=ws, player_guid=ME, player_name="Luaprata", player_position=position,
                           unexpected_disconnect=False, _sent=sent,
                           _send_packet=lambda opcode, payload=b"": sent.append(opcode))
    fc = FakeClock()
    sess._mover = mv.Mover(sess, ws, clock=fc.clock, sleep=real_sleep or fc.sleep)
    return sess


def make_observer(session):
    obs = AgentObserver("Luaprata")
    obs.attach(session)
    return obs


MOVE_OPCODES = {mv.MSG_MOVE_START_FORWARD, mv.MSG_MOVE_HEARTBEAT, mv.MSG_MOVE_STOP, mv.MSG_MOVE_SET_FACING}


class ValidateTest(unittest.TestCase):
    def check_bad(self, body, fragment):
        with self.assertRaises(control.ControlError) as cm:
            control.validate(body)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn(fragment, str(cm.exception))

    def test_needs_exactly_one_target_form(self):
        self.check_bad({}, "either x and y")
        self.check_bad({"x": 1, "y": 2, "near_player": "Rubens"}, "either x and y")
        self.check_bad({"x": 1}, "y is required")
        self.check_bad({"y": 1}, "x is required")

    def test_rejects_bad_numbers_and_unknown_fields(self):
        self.check_bad({"x": "1", "y": 2}, "finite number")
        self.check_bad({"x": True, "y": 2}, "finite number")
        self.check_bad({"x": float("nan"), "y": 2}, "finite number")
        self.check_bad({"x": 1e9, "y": 2}, "between")
        self.check_bad({"x": 1, "y": 2, "teleport": True}, "unknown field")
        self.check_bad({"x": 1, "y": 2, "timeout_s": 9999}, "timeout_s")
        self.check_bad({"x": 1, "y": 2, "stop_distance": 0}, "stop_distance")
        self.check_bad({"x": 1, "y": 2, "map": 1.5}, "integer")
        self.check_bad([], "JSON object")

    def test_rejects_hallucinated_player_names(self):
        self.check_bad({"near_player": "}}dots"}, "not a valid character name")
        self.check_bad({"near_player": 5}, "must be a string")

    def test_defaults(self):
        point = control.validate({"x": 1, "y": 2})
        self.assertEqual((point["kind"], point["stop_distance"], point["timeout_s"], point["z"]),
                         ("point", control.POINT_STOP_YD, control.DEFAULT_TIMEOUT_S, None))
        player = control.validate({"near_player": " Rubens "})
        self.assertEqual((player["kind"], player["name"], player["stop_distance"]),
                         ("player", "Rubens", control.PLAYER_STOP_YD))


class WalkTest(unittest.TestCase):
    def test_walk_to_a_point_arrives_and_reports_start_and_end(self):
        sess = make_session()
        status, out = control.walk(make_observer(sess), {"x": 50.0, "y": 20.0})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["outcome"], "arrived")
        self.assertIsNone(out["error"])
        self.assertEqual(out["start"], {"map": 530, "x": 10.0, "y": 20.0, "z": 30.0})
        self.assertAlmostEqual(out["end"]["x"], 50.0, delta=1.0)
        self.assertLessEqual(out["end_distance"], 1.0)
        self.assertAlmostEqual(out["moved"], 40.0, delta=1.5)
        self.assertEqual(out["target"], {"map": 530, "x": 50.0, "y": 20.0, "z": None})
        self.assertIn("move_to", out["method"])
        self.assertAlmostEqual(sess.player_position[1], out["end"]["x"], places=1)

    def test_only_the_characters_own_movement_packets_are_sent(self):
        sess = make_session()
        control.walk(make_observer(sess), {"x": 30.0, "y": 20.0})
        self.assertTrue(sess._sent)
        self.assertTrue(set(sess._sent) <= MOVE_OPCODES, sess._sent)
        self.assertIn(mv.MSG_MOVE_START_FORWARD, sess._sent)
        self.assertEqual(sess._sent[-1], mv.MSG_MOVE_STOP)

    def test_walk_next_to_a_perceived_player(self):
        sess = make_session()
        status, out = control.walk(make_observer(sess), {"near_player": "rubens"})  # case-insensitive
        self.assertEqual(status, 200, out)
        self.assertEqual(out["outcome"], "arrived")
        self.assertEqual(out["target"]["player"], "Rubens")
        self.assertLessEqual(out["end_distance"], control.PLAYER_STOP_YD)
        self.assertIn("move_towards", out["method"])

    def test_player_not_perceived_is_a_clear_error_and_nothing_moves(self):
        sess = make_session()
        status, out = control.walk(make_observer(sess), {"near_player": "Nobodyhere"})
        self.assertEqual(status, 404)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "player_not_perceived")
        self.assertIn("Nobodyhere is not perceived", out["error"])
        self.assertEqual(sess._sent, [])

    def test_a_creature_or_oneself_does_not_count_as_a_player(self):
        sess = make_session()
        for name in ("Wolf", "Luaprata"):
            status, out = control.walk(make_observer(sess), {"near_player": name})
            self.assertEqual((status, out["code"]), (404, "player_not_perceived"), name)

    def test_dry_run_plans_without_moving(self):
        sess = make_session()
        status, out = control.walk(make_observer(sess), {"near_player": "Rubens"}, dry_run=True)
        self.assertEqual(status, 200)
        self.assertEqual((out["outcome"], out["dry_run"], out["ok"]), ("dry_run", True, True))
        self.assertEqual(out["target"]["x"], 40.0)
        self.assertEqual(out["distance"], 30.0)
        self.assertEqual(out["start"], out["end"])
        self.assertEqual(sess._sent, [])
        self.assertEqual(sess.player_position, (530, 10.0, 20.0, 30.0, 0.0))

    def test_dry_run_still_validates(self):
        status, out = control.walk(make_observer(make_session()), {"near_player": "Nobodyhere"}, dry_run=True)
        self.assertEqual(status, 404)

    def test_blocked_walk_is_a_failure_with_the_reason(self):
        sess = make_session()
        stuck = ac.ActionResult(ok=False, error="stuck", detail={"reason": "no_progress"})
        with mock.patch.object(ac.REGISTRY["move_to"], "run", return_value=stuck):
            status, out = control.walk(make_observer(sess), {"x": 50.0, "y": 20.0})
        self.assertEqual(status, 422)
        self.assertFalse(out["ok"])
        self.assertEqual(out["outcome"], "blocked")
        self.assertIn("no_progress", out["error"])

    def test_target_lost_and_other_failures_are_not_success(self):
        sess = make_session()
        for error, outcome in (("target lost", "target_lost"), ("stopped", "stopped"), ("kaboom", "failed")):
            with mock.patch.object(ac.REGISTRY["move_towards"], "run",
                                   return_value=ac.ActionResult(ok=False, error=error)):
                status, out = control.walk(make_observer(sess), {"near_player": "Rubens"})
            self.assertEqual((status, out["ok"], out["outcome"]), (422, False, outcome), error)

    def test_timeout_stops_the_character_and_reports_timeout(self):
        sess = make_session(real_sleep=lambda dt: time.sleep(0.02))
        started = time.monotonic()
        status, out = control.walk(make_observer(sess), {"x": 1000.0, "y": 20.0, "timeout_s": 1})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(status, 422)
        self.assertEqual(out["outcome"], "timeout")
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])
        self.assertEqual(sess._sent[-1], mv.MSG_MOVE_STOP)   # it stopped, it did not keep running
        self.assertGreater(out["moved"], 0)

    def test_refusals_before_anything_moves(self):
        sess = make_session()
        obs = make_observer(sess)
        cases = [
            ({"x": 5000.0, "y": 20.0}, 400, "too_far"),
            ({"x": 50.0, "y": 20.0, "map": 1}, 409, "wrong_map"),
        ]
        for body, status, code in cases:
            got, out = control.walk(obs, body)
            self.assertEqual((got, out["code"], out["ok"]), (status, code, False), body)
        sess.world_state.get_my_object().health = 0
        self.assertEqual(control.walk(obs, {"x": 50.0, "y": 20.0})[1]["code"], "dead")
        sess.world_state.get_my_object().health = 100
        sess.player_position = None
        self.assertEqual(control.walk(obs, {"x": 50.0, "y": 20.0})[1]["code"], "no_position")
        obs.detach()
        self.assertEqual(control.walk(obs, {"x": 50.0, "y": 20.0})[1]["code"], "not_connected")
        self.assertEqual(sess._sent, [])

    def test_busy_when_the_think_loop_holds_the_lock(self):
        sess = make_session()
        obs = make_observer(sess)
        obs.action_lock.acquire()
        self.addCleanup(obs.action_lock.release)
        with mock.patch.object(control, "LOCK_WAIT_S", 0.05):
            status, out = control.walk(obs, {"x": 50.0, "y": 20.0})
        self.assertEqual((status, out["code"]), (409, "busy"))
        self.assertEqual(sess._sent, [])

    def test_the_lock_is_released_after_a_walk(self):
        obs = make_observer(make_session())
        control.walk(obs, {"x": 30.0, "y": 20.0})
        self.assertTrue(obs.action_lock.acquire(blocking=False))
        obs.action_lock.release()


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.observer = make_observer(self.session)
        self.observer.control_token = TOKEN
        self.server = make_server(self.observer, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, body, token=TOKEN, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_walk_over_http(self):
        status, out = self.post("/control/walk", {"x": 30.0, "y": 20.0})
        self.assertEqual((status, out["outcome"]), (200, "arrived"))

    def test_dry_run_query(self):
        status, out = self.post("/control/walk?dry_run=1", {"near_player": "Rubens"})
        self.assertEqual((status, out["outcome"]), (200, "dry_run"))
        self.assertEqual(self.session._sent, [])

    def test_player_not_perceived_over_http(self):
        status, out = self.post("/control/walk", {"near_player": "Nobodyhere"})
        self.assertEqual((status, out["code"]), (404, "player_not_perceived"))

    def test_wrong_or_missing_token_is_401_and_nothing_moves(self):
        for token in ("wrong", None):
            status, out = self.post("/control/walk", {"x": 30.0, "y": 20.0}, token=token)
            self.assertEqual(status, 401)
        self.assertEqual(self.session._sent, [])

    def test_bad_bodies_are_400(self):
        self.assertEqual(self.post("/control/walk", None, raw=b"not json")[0], 400)
        self.assertEqual(self.post("/control/walk", None, raw=b"")[0], 400)
        self.assertEqual(self.post("/control/walk", [1])[0], 400)
        self.assertEqual(self.post("/control/walk", {"x": 1})[0], 400)

    def test_other_post_paths_stay_405(self):
        self.assertEqual(self.post("/control/teleport", {"x": 1, "y": 2})[0], 405)
        self.assertEqual(self.post("/state", {})[0], 405)

    def test_no_token_configured_keeps_the_api_read_only(self):
        self.observer.control_token = ""
        status, out = self.post("/control/walk", {"x": 30.0, "y": 20.0})
        self.assertEqual(status, 405)
        self.assertEqual(self.session._sent, [])


if __name__ == "__main__":
    unittest.main()
