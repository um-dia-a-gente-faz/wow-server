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

LOGOUT_BOUND_S = session_mod.LOGOUT_WAIT_S * 2 + 1.0   # recv-thread join + logout wait + slack
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
        time.sleep(SETTLE_S)
        t0 = time.monotonic()
        sess.logout()
        took = time.monotonic() - t0
        try:
            self.assertLess(took, LOGOUT_BOUND_S)
            self.assertIsNone(sess.sock)
        finally:
            self.mock.close()
        self.assert_threads_return()

    def test_mock_close_stops_streaming_handlers(self):
        self.start()
        sess = _connect_and_login(self.cfg, LOG)
        self.addCleanup(sess.logout)
        time.sleep(SETTLE_S)
        self.mock.close()
        handlers = lambda: [t for t in threading.enumerate() if t.name.startswith("world-mock-handler")]
        self.assertTrue(wait_for(lambda: not handlers(), 3.0), f"handlers alive: {handlers()}")


if __name__ == "__main__":
    unittest.main()
