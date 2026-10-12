"""Run the agent suite in a random order to expose test-order dependences (#662).

    python3 -m agent.tests.shuffle_run SEED [test|module]

Shuffles per test (default) or per test module, with random.Random(SEED), runs
the result and exits non-zero on failure. Not a test module (no `test_` prefix).
"""

import os
import random
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USAGE = "usage: python3 -m agent.tests.shuffle_run SEED [test|module]"


def flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    out: list[unittest.TestCase] = []
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            out.extend(flatten(t))
        else:
            out.append(t)
    return out


def shuffled(seed: int, by: str = "test") -> unittest.TestSuite:
    tests = flatten(unittest.defaultTestLoader.discover(os.path.join(REPO, "agent", "tests"), top_level_dir=REPO))
    rng = random.Random(seed)
    if by == "module":
        groups: dict[str, list[unittest.TestCase]] = {}
        for t in tests:
            groups.setdefault(type(t).__module__, []).append(t)
        mods = list(groups.values())
        rng.shuffle(mods)
        tests = [t for g in mods for t in g]
    else:
        rng.shuffle(tests)
    return unittest.TestSuite(tests)


def main(argv: list[str]) -> int:
    by = argv[2] if len(argv) > 2 else "test"
    try:
        seed = int(argv[1])
    except (IndexError, ValueError):
        seed = None
    if seed is None or len(argv) > 3 or by not in ("test", "module"):
        print(USAGE, file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=0).run(shuffled(seed, by))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
