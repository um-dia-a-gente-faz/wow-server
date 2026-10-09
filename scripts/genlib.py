"""Shared by the generators under scripts/: write a generated file, or with --check
fail when the committed copy differs. Run everything with scripts/generate.py."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def dashboard_text(dash: dict) -> str:
    """Committed dashboard format: raw JSON (no import wrapper), indent 2, no trailing newline."""
    return json.dumps(dash, indent=2)


def sync(rel_path: str, text: str, check: bool, generator: str) -> int:
    path = REPO / rel_path
    if check:
        if not path.exists() or path.read_text() != text:
            print(f"{rel_path} is out of date: it is generated, do not edit it; "
                  f"run python3 scripts/generate.py (generator: {generator})", file=sys.stderr)
            return 1
        return 0
    path.write_text(text)
    print(f"wrote {rel_path}")
    return 0
