"""Run the agent suite in a random order to expose test-order dependences (#662).

    python3 -m agent.tests.shuffle_run SEED [test|module]

Shuffles per test (default) or per test module, with random.Random(SEED), runs
the result and exits non-zero on failure. Not a test module (no `test_` prefix).
"""

import random
import sys
import unittest


def flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    out: list[unittest.TestCase] = []
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            out.extend(flatten(t))
        else:
            out.append(t)
    return out


def shuffled(seed: int, by: str = "test") -> unittest.TestSuite:
    tests = flatten(unittest.defaultTestLoader.discover("agent/tests", top_level_dir="."))
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
    seed = int(argv[1])
    by = argv[2] if len(argv) > 2 else "test"
    result = unittest.TextTestRunner(verbosity=0).run(shuffled(seed, by))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
