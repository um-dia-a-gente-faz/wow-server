#!/usr/bin/env python3
"""Render docker-compose.agents.yml from agents/roster.json (UM-63).

    python3 scripts/gen_agents_compose.py            # rewrite docker-compose.agents.yml
    python3 scripts/gen_agents_compose.py --check    # exit 1 if the file is out of date

The roster is the source of truth; never edit the generated services by hand.
The HEADER and SERVICE templates and `render` live in agents/compose.py (#391): edit them
there, then regenerate. A test
(agent/tests/test_roster.py) fails when the committed compose file drifts.

Layout of the output:
  * one service per roster entry, named `agent-<lowercase character>`; the first
    PARTY_SIZE agents are in profiles [party, raid], the rest in [raid];
  * agent N publishes its read-only API on 9600 + N (9601..9625);
  * every container gets mem_limit/cpus from the x-agent defaults.

`docker compose -f docker-compose.agents.yml up -d agent-luaprata` still works
with profiles: naming a service explicitly enables its profiles.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO / "docker-compose.agents.yml"

sys.path.insert(0, str(REPO))
try:
    from agents import compose, roster  # noqa: E402
finally:
    sys.path.pop(0)

# Kept as names of this module: scripts/gen_agent_api_urls.py, tools/agent-runner and the tests read them here.
ROSTER_PATH = roster.ROSTER_PATH
FIRST_PORT = roster.FIRST_PORT
PARTY_SIZE = roster.PARTY_SIZE
DEFAULT_AUDIT_DIR = compose.DEFAULT_AUDIT_DIR
RAID_SIZE = compose.RAID_SIZE
HEADER = compose.HEADER
SERVICE = compose.SERVICE
JEV_MOCK = compose.JEV_MOCK
derive = compose.derive
render = compose.render


def load_roster(path: Path = ROSTER_PATH) -> list[dict]:
    """The validated entries as the five JSON keys; a malformed file raises roster.RosterError."""
    return [a.to_json() for a in roster.load_roster(path)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="exit 1 if docker-compose.agents.yml is stale")
    args = p.parse_args(argv)
    out = render(load_roster())
    if args.check:
        if COMPOSE_PATH.read_text() != out:
            print("docker-compose.agents.yml is out of date; run scripts/gen_agents_compose.py", file=sys.stderr)
            return 1
        return 0
    COMPOSE_PATH.write_text(out)
    print(f"wrote {COMPOSE_PATH.relative_to(REPO)} ({len(load_roster())} services)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
