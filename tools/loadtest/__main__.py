"""python -m tools.loadtest --agents 25 --duration 300 [--faults SPEC] [--out report.json]
[--compare baseline.json]. See tools/loadtest/README.md."""
import argparse
import json
import sys

from . import soak


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.loadtest", description=__doc__)
    ap.add_argument("--agents", type=int, default=5)
    ap.add_argument("--duration", type=float, default=60.0, help="seconds")
    ap.add_argument("--faults", default="", help="MOCK_FAULTS spec, repeated for the whole run")
    ap.add_argument("--think-interval", type=float, default=0.1)
    ap.add_argument("--host", default="127.0.0.1", help="loopback only")
    ap.add_argument("--allow-non-loopback", action="store_true", help="the mock still runs locally")
    ap.add_argument("--out", help="also write the JSON report here")
    ap.add_argument("--compare", help="baseline report; exit 1 on a per-agent regression")
    ap.add_argument("--tolerance", type=float, default=0.2)
    a = ap.parse_args(argv)
    rep = soak.run_soak(a.agents, a.duration, a.faults, a.think_interval, a.host,
                        allow_non_loopback=a.allow_non_loopback)
    text = json.dumps(rep, indent=1)
    print(text)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
    if a.compare:
        bad = soak.compare(rep, soak.load(a.compare), a.tolerance)
        for line in bad:
            print("REGRESSION", line, file=sys.stderr)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
