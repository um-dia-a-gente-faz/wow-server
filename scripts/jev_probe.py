#!/usr/bin/env python3
"""One real Jev decision against the JEV_* config in the environment (#164).

    set -a; . /opt/wow-server/.env; set +a
    python3 scripts/jev_probe.py

Prints the chosen option, confidence, latency and usage, plus the raw response
body, so the endpoint's real shape can be recorded. The API key is never
printed. Exits 1 if Jev is not configured or the call fails.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.config import Config  # noqa: E402
from agent.jev import JevClient, JevError  # noqa: E402

SNAPSHOT = {"self": {"name": "probe", "hp_pct": 100, "in_combat": False}, "nearby": []}
CANDIDATES = [
    {"action": "idle", "params": {}, "why": "stand still"},
    {"action": "say", "params": {"text": "hello"}, "why": "greet nearby players"},
    {"action": "wander", "params": {"distance": 10}, "why": "walk a short way"},
]


def main() -> int:
    cfg = Config()
    if not cfg.jev_enabled:
        print("Jev is off: set JEV_BASE_URL (and JEV_API_KEY) in the environment", file=sys.stderr)
        return 1
    client = JevClient(cfg.jev_base_url, cfg.jev_model, api_key=cfg.jev_api_key)
    raw = {}
    orig = client._post

    def capture(path, body):
        raw.update(orig(path, body))
        return raw

    client._post = capture
    print(f"endpoint {cfg.jev_base_url}/decisions model {cfg.jev_model} "
          f"key {'set' if cfg.jev_api_key else 'unset'}")
    try:
        action, params = client.choose_action(SNAPSHOT, CANDIDATES)
    except JevError as e:
        print(f"FAILED: {e} (status {e.status})", file=sys.stderr)
        return 1
    print(f"choice {client.last_choice} -> {action} {params}")
    print(f"confidence {client.last_confidence} latency_ms {client.last_latency_ms:.0f}")
    print(f"usage {json.dumps(client.last_usage)}")
    print("raw response:", json.dumps(raw, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
