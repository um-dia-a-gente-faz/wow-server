"""#714: scripts/early_failures.py. Failures print as they happen; the stall sequence; the exit codes.

Everything that writes does so under a temporary directory.
"""
import io
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "early_failures.py"
sys.path.insert(0, str(ROOT / "scripts"))

import early_failures as ef  # noqa: E402


class ImmediatePrintTest(unittest.TestCase):
    def run_suite(self):
        """Run fails, errors, skipped, xfail, subtest, probe in order; the probe copies the stream as it is by then."""
        buf = io.StringIO()
        seen = []

        class T(unittest.TestCase):
            def test_a_fails(self):
                self.fail("boom-fail")

            def test_b_errors(self):
                raise ValueError("boom-error")

            @unittest.skip("skipped-reason")
            def test_c_skipped(self):
                pass

            @unittest.expectedFailure
            def test_d_xfail(self):
                self.fail("boom-xfail")

            def test_e_subtest(self):
                with self.subTest(i=1):
                    self.fail("boom-sub")

            def test_f_probe(self):
                seen.append(buf.getvalue())

        names = sorted(n for n in dir(T) if n.startswith("test_"))
        runner = unittest.TextTestRunner(stream=buf, resultclass=ef.EarlyResult)
        try:
            result = runner.run(unittest.TestSuite(T(n) for n in names))
        finally:
            ef.EarlyResult.live = None
        return seen[0], buf.getvalue(), result

    def test_failure_error_and_subtest_failure_are_printed_once_before_the_next_test_runs(self):
        before, _, _ = self.run_suite()
        for marker in ("AssertionError: boom-fail", "ValueError: boom-error", "AssertionError: boom-sub"):
            self.assertEqual(before.count(marker), 1, marker)   # the traceback, once; no summary has run yet
        self.assertRegex(before, r"FAIL: .*test_a_fails")
        self.assertRegex(before, r"ERROR: .*test_b_errors")
        self.assertRegex(before, r"FAIL: .*test_e_subtest")

    def test_skips_and_expected_failures_print_nothing(self):
        before, _, _ = self.run_suite()
        for text in ("skipped-reason", "boom-xfail", "test_c_skipped", "test_d_xfail"):
            self.assertNotIn(text, before)

    def test_unittests_own_summary_still_follows(self):
        _, full, result = self.run_suite()
        self.assertIn("FAILED (failures=2, errors=1, skipped=1, expected failures=1)", full)
        self.assertEqual((len(result.failures), len(result.errors)), (2, 1))


class StallStopTest(unittest.TestCase):
    def stub(self, exits_on=None):
        """A Popen whose wait() times out until signal `exits_on` was sent (never if None). Records every call."""
        calls = []
        p = mock.Mock()

        def wait(timeout=None):
            calls.append(("wait", timeout))
            if exits_on is not None and ("send", exits_on) in calls:
                return 7
            if timeout is None:
                return -9
            raise subprocess.TimeoutExpired("x", timeout)

        p.send_signal.side_effect = lambda s: calls.append(("send", s))
        p.kill.side_effect = lambda: calls.append(("kill",))
        p.wait.side_effect = wait
        return p, calls

    def stop(self, p):
        with mock.patch("sys.stderr", io.StringIO()):
            return ef.stall_stop(p)

    def test_a_process_that_ignores_everything_gets_each_signal_and_its_wait_then_kill(self):
        p, calls = self.stub()
        self.stop(p)
        S = signal
        self.assertEqual(calls, [
            ("send", S.SIGUSR1), ("wait", 2),
            ("send", S.SIGUSR1), ("wait", 2),
            ("send", S.SIGINT), ("wait", 10),
            ("send", S.SIGABRT), ("wait", 10),
            ("kill",), ("wait", None)])

    def test_a_process_that_exits_after_sigint_is_not_sent_sigabrt_or_killed(self):
        p, calls = self.stub(exits_on=signal.SIGINT)
        self.assertEqual(self.stop(p), 7)
        self.assertEqual([c[1] for c in calls if c[0] == "send"], [signal.SIGUSR1, signal.SIGUSR1, signal.SIGINT])
        p.kill.assert_not_called()

    def test_the_soft_stop_is_the_dumps_and_the_interrupt_wait(self):
        self.assertEqual(ef.SOFT_STOP_S, 2 + 2 + 10)


class RealProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_dir(self, body):
        d = pathlib.Path(tempfile.mkdtemp(dir=self.tmp.name))
        (d / "test_x.py").write_text(textwrap.dedent(body))
        return str(d)

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True,
                              timeout=60, env={**os.environ, "PYTHONFAULTHANDLER": "1"})

    def test_exit_codes_match_python_m_unittest(self):
        bodies = {
            "pass": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n",
            "fail": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.fail('x')\n",
            "none": "import unittest\n",
        }
        codes = set()
        for name, body in bodies.items():
            with self.subTest(name):
                d = self.make_dir(body)
                ref = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", d], cwd=ROOT,
                                     capture_output=True, text=True)
                ours = self.run_script("discover", "-s", d)
                self.assertEqual(ours.returncode, ref.returncode)
                codes.add(ours.returncode)
        self.assertEqual(codes, {0, 1, 5})   # pass, failures, no tests ran (3.12)

    def test_a_stall_shows_the_failure_then_the_dump_naming_the_test_then_the_summary(self):
        d = self.make_dir("""
            import threading, unittest
            class T(unittest.TestCase):
                def test_a_fails(self):
                    self.fail("early-failure-marker")
                def test_b_blocks_forever(self):
                    threading.Event().wait()
                def test_c_never_runs(self):
                    pass
            """)
        t0 = time.monotonic()
        r = self.run_script("--stall-after", "1", "--", "discover", "-s", d)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(r.returncode, 1)
        err = r.stderr
        stall = err.index("still running after 1 s")
        self.assertLess(err.index("early-failure-marker"), stall)     # printed when it failed, before the stall
        dump = err[stall:err.index("stall: sending SIGINT")]
        self.assertIn("test_b_blocks_forever", dump)                  # the first (non-fatal) dump names the test
        self.assertEqual(dump.count("most recent call first"), 2)     # two dumps
        self.assertIn("Stopped: stopped by SIGINT", err)
        self.assertIn("FAILED (failures=1, errors=1)", err)           # unittest's summary was reached
        self.assertNotIn("stall: sending SIGABRT", err)

    def test_a_normal_run_prints_dots_and_the_summary_only(self):
        d = self.make_dir("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        r = self.run_script("--stall-after", "30", "--", "discover", "-s", d)
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stderr, r"^\.\n-+\nRan 1 test in [\d.]+s\n\nOK\n$")


if __name__ == "__main__":
    unittest.main()
