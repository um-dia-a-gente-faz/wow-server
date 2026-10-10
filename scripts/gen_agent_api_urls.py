#!/usr/bin/env python3
"""Render wowmap's AGENT_API_URLS from agents/roster.json (GH #132).

    python3 scripts/gen_agent_api_urls.py --host wow-agents.lan
    AGENT_HOST=10.0.0.7 python3 scripts/gen_agent_api_urls.py

Prints one line, `AGENT_API_URLS=Name=http://<host>:<port>,...`, in roster order.
Agent N (1-based) serves its read-only API on 9600+N, exactly as
docker-compose.agents.yml publishes it: the roster loader and the port base are
imported from gen_agents_compose.py, so the two cannot drift (a test also checks
the result against the committed compose file).

The host is deliberately required, with no default: the agent fleet no longer
shares a machine with the realm (192.168.1.64), it moves to its own `wow-agents`
VM (ADR 0002 / issue #134), and a wrong default would silently point wowmap's
Agent mind tab at nothing. Give it with --host or the AGENT_HOST env var.

This only prints. Putting the line into /opt/wow-server/.env on the wow-server VM
and recreating wowmap (`docker compose --env-file .env -f monitoring/docker-compose.yml
up -d wowmap`) is a manual step; see docs/DEPLOYMENT.md.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_agents_compose as gen  # noqa: E402

# A DNS name or IPv4 address; anything with '=', ',', '/', ':' or spaces would
# corrupt the "Name=url,Name=url" list that wowmap splits on. fullmatch, not
# match: `$` alone would let a trailing newline through into the .env line.
HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")


def render(agents: list[dict], host: str) -> str:
    if not HOST_RE.fullmatch(host or ""):
        raise ValueError(f"bad agent host {host!r}: want a hostname or IPv4 address, no port or scheme")
    names = [a["character"] for a in agents]
    if len({n.lower() for n in names}) != len(names):
        raise ValueError("duplicate character name in roster")
    return ",".join(f"{name}=http://{host}:{gen.FIRST_PORT + i}" for i, name in enumerate(names))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true",
                   help="exit 1 if the names/ports differ from docker-compose.agents.yml (needs no host)")
    p.add_argument("--host", default=os.environ.get("AGENT_HOST"),
                   help="host the agent containers run on (or set AGENT_HOST); required, no default")
    args = p.parse_args(argv)
    if args.check:
        services = gen.COMPOSE_PATH.read_text().split("\n  agent-")  # one block per service
        for pair in render(gen.load_roster(), "check.invalid").split(","):
            name, url = pair.split("=")
            port = url.rsplit(":", 1)[1]
            if not any(f"WOW_CHARACTER: {name}\n" in s and f"AGENT_HTTP_PORT: {port}\n" in s for s in services):
                print(f"{pair} disagrees with docker-compose.agents.yml: it is generated; "
                      "run python3 scripts/generate.py", file=sys.stderr)
                return 1
        return 0
    if not args.host:
        p.error("the agent host is required: pass --host or set AGENT_HOST "
                "(it is not 192.168.1.64 once the fleet moves to the wow-agents VM)")
    try:
        value = render(gen.load_roster(), args.host)
    except ValueError as e:
        p.error(str(e))
    print(f"AGENT_API_URLS={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
