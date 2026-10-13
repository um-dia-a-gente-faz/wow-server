#!/usr/bin/env python3
"""Entry point of the CI test runs instead of `python3 -m unittest` (#714). Same arguments, same exit codes.

  python3 scripts/early_failures.py discover -s agent/tests
  python3 scripts/early_failures.py --stall-after 420 -- discover -s agent/tests

1. A failure or error prints its id and traceback to stderr the moment it happens (unittest only
   prints them in its final summary, which a killed run never reaches); dots and summary are unchanged.
2. The process registers SIGUSR1 as a non-fatal dump of every thread's stack and, only when
   CHECK_STALL_DUMP_S is set, SIGINT as a soft
   stop: the running test fails with Stopped, the rest are skipped, unittest prints its summary.
3. --stall-after N runs the real process and, past N seconds, walks stall_stop(): dump, dump, SIGINT,
   SIGABRT (a fatal dump when PYTHONFAULTHANDLER=1), kill. scripts/coverage_floor.py uses stall_stop too.

stdlib only. coverage's --source=agent keeps this file out of the measured set.
"""
import faulthandler
import os
import signal
import subprocess
import sys
import unittest

# (signal, seconds to wait for the process to exit before the next one)
STEPS = ((signal.SIGUSR1, 2), (signal.SIGUSR1, 2), (signal.SIGINT, 10), (signal.SIGABRT, 10))
# Seconds from the bound to SIGABRT; the kill follows at most 10 s later (24 s in all, the worst case CI keeps free).
SOFT_STOP_S = sum(wait for sig, wait in STEPS if sig != signal.SIGABRT)


class Stopped(BaseException):
    """Raised in the running test by SIGINT under a stall bound. A BaseException so that a test's
    `except Exception` cannot swallow the stop; `except BaseException` still can, and then SIGABRT's
    fatal dump (the next step) is the fallback."""


class EarlyResult(unittest.TextTestResult):
    live: "EarlyResult | None" = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        EarlyResult.live = self

    def _show(self, kind, entries):
        test, tb = entries[-1]
        self.stream.write(f"\n{kind}: {test.id()}\n{tb}")
        self.stream.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._show("FAIL", self.failures)

    def addError(self, test, err):
        super().addError(test, err)
        self._show("ERROR", self.errors)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:   # TestResult appends to the lists itself, not through addFailure/addError
            fail = issubclass(err[0], test.failureException)
            self._show("FAIL" if fail else "ERROR", self.failures if fail else self.errors)


class EarlyRunner(unittest.TextTestRunner):
    resultclass = EarlyResult   # a class, so unittest builds it from the command line (-v, -f, -b, warnings)


def _soft_stop(sig, frame):
    signal.signal(sig, signal.default_int_handler)   # a second one is a plain KeyboardInterrupt
    if EarlyResult.live is None:
        raise KeyboardInterrupt
    EarlyResult.live.stop()
    raise Stopped("stopped by SIGINT: the run is past its stall deadline")


def stall_stop(p: subprocess.Popen) -> int:
    """Walk STEPS on a process past its deadline, stopping at the first that makes it exit; kill as the last."""
    for sig, wait in STEPS:
        print(f"\nstall: sending {sig.name}", file=sys.stderr, flush=True)
        p.send_signal(sig)
        try:
            return p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            pass
    p.kill()
    return p.wait()


def supervise(seconds: float, args: list[str]) -> int:
    # the bound in the child's environment is what arms its SIGINT handler (see main)
    p = subprocess.Popen([sys.executable, __file__, *args], env={**os.environ, "CHECK_STALL_DUMP_S": f"{seconds:g}"})
    try:
        return p.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        print(f"\nstall: still running after {seconds:g} s", file=sys.stderr, flush=True)
        stall_stop(p)
        return 1
    except BaseException:
        p.kill()
        p.wait()
        raise


def main(argv: list[str]) -> int:
    if argv[:1] == ["--stall-after"]:
        if argv[2:3] != ["--"]:
            print("usage: early_failures.py [--stall-after N --] <unittest args>", file=sys.stderr)
            return 2
        return supervise(float(argv[1]), argv[3:])
    sys.path[0] = os.getcwd()   # what `python -m unittest` has: the repo root, not scripts/
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    if os.environ.get("CHECK_STALL_DUMP_S"):   # a stall bound is in force: SIGINT is the soft stop, not Ctrl-C
        signal.signal(signal.SIGINT, _soft_stop)
    unittest.main(module=None, argv=["python -m unittest", *argv], testRunner=EarlyRunner)
    return 0   # unreachable: unittest.main exits


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
