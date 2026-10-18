"""agent.movement.Mover on a sidecar route (#312): waypoints from a stub tools/pathfinder, the
straight-line fallback, no_path, and one stop ending the whole route."""

import math
import socket
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import movement as mv
from agent import packets as pk
from agent import perception as per
from agent.pathing import PathClient
from agent.tests.test_pathing import StubSidecar, ok_reply
from agent.tests.test_movement import FakeClock

MAP = 530
START = (0.0, 0.0, 0.0)
END = (20.0, 0.0, 0.0)
# A wall at y=0 between x=5 and x=15: the sidecar's detour goes up to y=10 and back.
DETOUR = [START, (0.0, 10.0, 5.0), (20.0, 10.0, 5.0), END]


def fake_session(path_client=None):
    sent = []
    sess = SimpleNamespace(player_guid=0xF130000000000099, player_position=(MAP, *START, 0.0),
                           mover=None, path_client=path_client, events=[])
    sess.send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess.record_event = lambda kind, **fields: sess.events.append({"kind": kind, **fields})
    sess._sent = sent
    return sess


def heartbeat_positions(sess):
    out = []
    for opcode, payload in sess._sent:
        if opcode == mv.MSG_MOVE_HEARTBEAT:
            _, off = pk.unpack_packed_guid(payload, 0)
            x, y, z, _o = struct.unpack_from('<4f', payload, off + 4 + 2 + 4)
            out.append((x, y, z))
    return out


def opcodes_of(sess, opcode):
    return [op for op, _ in sess._sent if op == opcode]


def near(a, b, yd=1.0):
    """Within `yd` on the ground plane: the arrival test _simulate uses (its z is interpolated)."""
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= yd


class RouteTest(unittest.TestCase):
    def sidecar(self, reply):
        stub = StubSidecar(reply)
        self.addCleanup(stub.close)
        return stub

    def move(self, sess):
        clock = FakeClock()
        mover = mv.Mover(sess, per.WorldState(), clock=clock.clock, sleep=clock.sleep)
        return mover, mover.move_to(END[0], END[1], stop_distance=1.0, run_speed=7.0, tick_interval=0.1)

    def test_walks_the_detour_in_order_and_reports_waypoints(self):
        stub = self.sidecar(lambda req: ok_reply(DETOUR))
        sess = fake_session(PathClient(stub.url))
        _, result = self.move(sess)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["detail"]["waypoints"], 3)  # DETOUR minus the start point
        self.assertEqual(sess.events, [])  # a served route records no fallback
        self.assertEqual(stub.requests[0]["map"], MAP)
        positions = heartbeat_positions(sess)
        cursor = 0
        for wp in DETOUR[1:]:
            hit = next((i for i in range(cursor, len(positions)) if near(positions[i], wp)), None)
            self.assertIsNotNone(hit, f"never passed within 1 yd of {wp} in order")
            cursor = hit
        self.assertTrue(near(positions[-1], END))

    def test_no_path_fails_without_moving(self):
        stub = self.sidecar(lambda req: (200, b'{"status": "no_path", "points": []}'))
        sess = fake_session(PathClient(stub.url))
        _, result = self.move(sess)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no path")
        self.assertEqual(result["detail"], {"reason": "no_path"})
        self.assertEqual(sess._sent, [])
        self.assertEqual(sess.events, [])

    def test_sidecar_down_falls_back_to_straight_line_and_records_once(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assert_fallback(PathClient(f"http://127.0.0.1:{port}"))

    def test_sidecar_500_falls_back_to_straight_line_and_records_once(self):
        stub = self.sidecar(lambda req: (500, b'{"error": "internal error"}'))
        self.assert_fallback(PathClient(stub.url))

    def assert_fallback(self, client):
        sess = fake_session(client)
        _, result = self.move(sess)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("waypoints", result["detail"])
        self.assertTrue(near(heartbeat_positions(sess)[-1], END))
        fallbacks = [e for e in sess.events if e["kind"] == "path_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertTrue(fallbacks[0]["reason"])

    def test_no_pathfinder_url_means_no_http_and_no_event(self):
        sess = fake_session(path_client=None)
        with mock.patch("urllib.request.urlopen") as urlopen:
            _, result = self.move(sess)
        urlopen.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(sess.events, [])
        self.assertTrue(near(heartbeat_positions(sess)[-1], END))

    def test_caller_stop_distance_applies_to_the_last_segment_only(self):
        stub = self.sidecar(lambda req: ok_reply(DETOUR))
        sess = fake_session(PathClient(stub.url))
        clock = FakeClock()
        mover = mv.Mover(sess, per.WorldState(), clock=clock.clock, sleep=clock.sleep)
        result = mover.move_to(END[0], END[1], stop_distance=5.0, run_speed=7.0, tick_interval=0.1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["detail"]["waypoints"], 3)
        # Stopped on the 5-yd arrival ring of END, not on the 1-yd corner rule.
        final = heartbeat_positions(sess)[-1]
        self.assertGreater(math.hypot(final[0] - END[0], final[1] - END[1]), 4.0)
        self.assertLessEqual(math.hypot(final[0] - END[0], final[1] - END[1]), 5.0)

    def test_stop_between_segments_ends_the_route_before_segment_two(self):
        stub = self.sidecar(lambda req: ok_reply(DETOUR))
        sess = fake_session(PathClient(stub.url))
        clock = FakeClock()
        mover = mv.Mover(sess, per.WorldState(), clock=clock.clock, sleep=clock.sleep)
        send = sess.send_packet

        def stop_when_segment_one_ends(opcode, payload=b''):
            send(opcode, payload)
            if opcode == mv.MSG_MOVE_STOP:  # segment 1's arrival stop: stop lands between segments
                mover._stop_event.set()

        sess.send_packet = stop_when_segment_one_ends
        result = mover.move_to(END[0], END[1], stop_distance=1.0, run_speed=7.0, tick_interval=0.1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stopped")
        self.assertEqual(result["detail"]["waypoint"], 1)
        self.assertEqual(len(opcodes_of(sess, mv.MSG_MOVE_START_FORWARD)), 1)  # segment 2 never started

    def test_stop_during_segment_two_ends_the_route(self):
        stub = self.sidecar(lambda req: ok_reply(DETOUR))
        sess = fake_session(PathClient(stub.url))
        mover_box = {}
        fired = []

        def stop_in_segment_two():
            # The second START_FORWARD marks segment 2; stop from another thread, as a reflex would.
            if len(opcodes_of(sess, mv.MSG_MOVE_START_FORWARD)) == 2 and not fired:
                fired.append(True)
                threading.Thread(target=mover_box["m"].stop).start()

        clock = FakeClock()

        def sleep(dt):
            clock.sleep(dt)
            stop_in_segment_two()

        mover = mv.Mover(sess, per.WorldState(), clock=clock.clock, sleep=sleep)
        mover_box["m"] = mover
        result = mover.move_to(END[0], END[1], stop_distance=1.0, run_speed=7.0, tick_interval=0.1)

        self.assertEqual(fired, [True])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stopped")
        self.assertEqual(result["detail"]["waypoint"], 1)
        self.assertEqual(len(opcodes_of(sess, mv.MSG_MOVE_START_FORWARD)), 2)  # no segment 3


if __name__ == "__main__":
    unittest.main()
