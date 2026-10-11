#!/usr/bin/env python3
"""Per-module error baseline for the legacy tier of mypy.ini (#378).

A legacy module (`ignore_errors = True` in mypy.ini) hides its errors from `mypy`. This script
runs mypy on a copy of mypy.ini with those lines removed, counts the errors of each legacy
module, and compares the counts with mypy-legacy-baseline.json. A count may fall (then run
--update) and must not rise.

  python3 scripts/mypy_legacy_baseline.py           # --check (default)
  python3 scripts/mypy_legacy_baseline.py --update  # rewrite the baseline from this run
"""
import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "mypy-legacy-baseline.json"
ERROR_LINE = re.compile(r"^(?P<path>[^:\n]+):\d+: error: ")
SUMMARY = re.compile(r"^(Success: no issues found|Found \d+ errors? in \d+ files?)", re.MULTILINE)
IGNORE_ERRORS = re.compile(r"^\s*ignore[_-]errors\s*=", re.IGNORECASE)


def load_ini(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return cp


def legacy_modules(ini_text):
    """Module names of the mypy.ini sections that set ignore_errors = True."""
    cp = load_ini(ini_text)
    out = set()
    for section in cp.sections():
        if not section.startswith("mypy-"):
            continue
        for key, value in cp[section].items():
            if key.replace("-", "_") == "ignore_errors" and cp.BOOLEAN_STATES.get(value.strip().lower(), False):
                out.add(section[len("mypy-"):])
    return out


def mypy_path_bases(ini_text):
    """Directories mypy names modules from: the mypy_path entries plus the cwd."""
    cp = load_ini(ini_text)
    if not cp.getboolean("mypy", "explicit_package_bases", fallback=False):
        raise SystemExit("mypy.ini must set explicit_package_bases = True: module names depend on it")
    raw = cp.get("mypy", "mypy_path", fallback="")
    bases = [os.path.normpath(p.strip()) for p in re.split(r"[:,]", raw) if p.strip()]
    return bases + ["."]


def module_of(path, bases):
    """Module name mypy gives `path` (relative to the repo root): the longest base that contains it."""
    parts = PurePosixPath(os.path.normpath(path).replace(os.sep, "/")).parts
    best = None  # (depth of the base, path parts below it)
    for base in bases:
        b = () if base == "." else PurePosixPath(base).parts
        if parts[:len(b)] == b and (best is None or len(b) > best[0]):
            best = (len(b), parts[len(b):])
    names = list(best[1])
    names[-1] = names[-1].removesuffix(".py")
    if names[-1] == "__init__":
        names.pop()
    return ".".join(names)


def count_legacy_errors(output, legacy, bases):
    """Errors per legacy module in mypy's output text; a legacy module with none counts 0."""
    counts = dict.fromkeys(legacy, 0)
    for line in output.splitlines():
        m = ERROR_LINE.match(line)
        if m:
            module = module_of(m.group("path"), bases)
            if module in counts:
                counts[module] += 1
    return counts


def unlegacy_config(ini_text):
    """mypy.ini without its ignore_errors lines, so the legacy modules are checked too."""
    return "".join(line for line in ini_text.splitlines(keepends=True) if not IGNORE_ERRORS.match(line))


def compare(baseline, now):
    """(failures, notes) of the current counts against the committed baseline."""
    failures, notes = [], []
    for module in sorted(now):
        if module not in baseline:
            failures.append(f"{module}: not in mypy-legacy-baseline.json (add it with --update)")
        elif now[module] > baseline[module]:
            failures.append(f"{module}: baseline {baseline[module]}, now {now[module]}")
        elif now[module] < baseline[module]:
            notes.append(f"{module}: baseline {baseline[module]}, now {now[module]} (lower it with --update)")
    for module in sorted(set(baseline) - set(now)):
        failures.append(f"{module}: no longer a legacy module (remove it with --update)")
    return failures, notes


def run_mypy(ini_text):
    """mypy's output on the un-legacied config, run from the repo root. Exit unless mypy ran in full.

    PYTHONNOUSERSITE=1 keeps the developer's ~/.local packages (Pillow, PyMySQL) out, as in the
    CI lint job, which installs only requirements-dev.txt: they change the wowmap counts.
    """
    env = {**os.environ, "PYTHONNOUSERSITE": "1"}
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp, "mypy.ini")
        config.write_text(unlegacy_config(ini_text))
        proc = subprocess.run(
            ["mypy", "--config-file", str(config), "--cache-dir", str(Path(tmp, "cache"))],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )
    return full_run(proc.returncode, proc.stdout, proc.stderr)


def full_run(returncode, stdout, stderr=""):
    """mypy's output when mypy ran in full: exit 0 (clean) or 1 (errors) and a summary line.

    Anything else would make the counts meaningless: an empty output would pass as zero.
    """
    if returncode not in (0, 1) or not SUMMARY.search(stdout):
        sys.stderr.write(stdout + stderr)
        raise SystemExit(f"mypy exited {returncode} without a summary: no error counts")
    return stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description="Per-module error baseline for mypy.ini's legacy tier.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if a legacy module gained errors (default)")
    mode.add_argument("--update", action="store_true", help="rewrite mypy-legacy-baseline.json")
    args = parser.parse_args(argv)

    ini = (ROOT / "mypy.ini").read_text()
    now = count_legacy_errors(run_mypy(ini), legacy_modules(ini), mypy_path_bases(ini))
    if args.update:
        BASELINE.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
        print(f"wrote {BASELINE.name}: {len(now)} legacy modules, {sum(now.values())} errors")
        return 0

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    failures, notes = compare(baseline, now)
    for note in notes:
        print(f"note: {note}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print(f"legacy error baseline ok: {len(now)} legacy modules, {sum(now.values())} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
