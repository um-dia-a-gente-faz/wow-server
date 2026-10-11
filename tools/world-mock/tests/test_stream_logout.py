"""#485: a world socket that keeps streaming after a bad-crypt fault must not hang
WoWSession.logout(), and WorldMock.close() must stop handlers that are still streaming."""

import logging
import pathlib
import sys
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from server import FaultPlan  # noqa: E402

from agent import session as session_mod  # noqa: E402
from agent.__main__ import _connect_and_login  # noqa: E402

LOGOUT_BOUND_S = session_mod.LOGOUT_WAIT_S * 2 + 3.0   # recv-thread join + logout wait + 3 s slack
LOG = logging.getLogger("test")
STREAM_HZ = 200.0       # fast enough that bytes always arrive, the socket timeout never fires
SETTLE_S = 0.5          # let the desync happen and the stream fill the socket buffers


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class StreamingShutdownTest(unittest.TestCase):
    def setUp(self):
        self.threads_before = threading.active_count()

    def start(self, *faults):
        self.mock = server.WorldMock(faults=faults, stream_hz=STREAM_HZ)
        self.cfg = SimpleNamespace(
            wow_host="127.0.0.1", wow_auth_port=self.mock.auth_port, account=server.DEFAULT_ACCOUNT,
            password=server.DEFAULT_PASSWORD, verbose_packets=False, dump_packets_dir="",
            character=None, char_guid=0, channels="none", think_interval=0.02)

    def assert_threads_return(self):
        self.assertTrue(wait_for(lambda: threading.active_count() <= self.threads_before),
                        f"thread leak: {threading.enumerate()}")

    def test_logout_is_bounded_with_stream_and_bad_crypt(self):
        self.start(FaultPlan(bad_crypt_from=9))     # after the login burst, once the stream runs
        sess = _connect_and_login(self.cfg, LOG)
        sock = sess.sock
        self.addCleanup(self.assert_threads_return)     # registered first, runs after the closes below
        self.addCleanup(self.mock.close)
        self.addCleanup(sock.close)     # a hung logout() thread must not keep the process alive
        time.sleep(SETTLE_S)
        t = threading.Thread(target=sess.logout, daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(LOGOUT_BOUND_S)
        self.assertFalse(t.is_alive(), f"logout() still blocked after {LOGOUT_BOUND_S} s")
        self.assertLess(time.monotonic() - t0, LOGOUT_BOUND_S)
        self.assertIsNone(sess.sock)

    def test_mock_close_stops_streaming_handlers(self):
        self.start()
        sess = _connect_and_login(self.cfg, LOG)
        self.addCleanup(sess.logout)
        time.sleep(SETTLE_S)

        def handlers():     # by target, so this also finds them on a server.py without the thread name
            return [t for t in threading.enumerate() if getattr(t, "_target", None) == self.mock._guarded]

        self.assertTrue(handlers(), "no handler thread found while the client is connected")
        self.mock.close()
        self.assertTrue(wait_for(lambda: not handlers(), 3.0), f"handlers alive: {handlers()}")


if __name__ == "__main__":
    unittest.main()
