#!/usr/bin/env python3
"""Regenerate every generated file, or with --check fail when one is out of date (#297).

    python3 scripts/generate.py            # rewrite all generated files
    python3 scripts/generate.py --check    # exit 1 naming each stale generator (CI)

A new generator is one line in GENERATORS: a command that rewrites its outputs, and with
`--check` appended exits non-zero on drift (scripts/genlib.py does the compare for file writers).
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

GENERATORS = [
    ["scripts/gen_agents_compose.py"],         # docker-compose.agents.yml <- agents/roster.json
    ["scripts/gen_agent_api_urls.py"],         # AGENT_API_URLS (prints; --check compares to the compose file)
    ["scripts/gen-live-map-dash.py"],          # monitoring/grafana-dashboard-wow-live-map.json
    ["scripts/gen-wow-dashboard.py"],          # monitoring/grafana-dashboard-wow-server-host.json
    ["scripts/gen-wow-game-dashboards.py"],    # ...-wow-players.json, ...-wow-realm-health.json
    ["-m", "agent.api_contract"],              # docs/AGENT-API.md <- agent/api_schema.json
]


def main(argv=None) -> int:
    check = "--check" in (sys.argv[1:] if argv is None else argv)
    failed = []
    for gen in GENERATORS:
        if gen == ["scripts/gen_agent_api_urls.py"] and not check:
            continue  # prints one line and needs --host; nothing to write
        if subprocess.run([sys.executable, *gen, *(["--check"] if check else [])], cwd=REPO).returncode:
            failed.append(" ".join(gen))
    if failed:
        print("generated files out of date, run python3 scripts/generate.py. Failed: " + "; ".join(failed),
              file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
