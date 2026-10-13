#!/usr/bin/env bash
# Single entry point shared by CI and contributors (#298).
#   scripts/check.sh <target>...   targets: compile lint types types-legacy generated test test-tools test-<suite> all
#   coverage = the agent coverage gate (#384); not part of all, it needs `coverage` installed.
# Add a test suite = add one line to SUITES. scripts/tests guards that no tests dir is missed.
set -euo pipefail
cd "$(dirname "$0")/.."

# name|command  (the command runs from the repo root)
SUITES=(
  "agent|python3 -m unittest discover -t . -s agent/tests"
  "chat-feed|python3 -m unittest discover -s tools/chat-feed/tests"
  "common|python3 -m unittest discover -s tools/common/tests"
  "tools-arch|python3 -m unittest discover -s tools/tests"
  "dbc|python3 -m unittest discover -s tools/dbc/tests"
  "world-mock|python3 -m unittest discover -s tools/world-mock/tests"
  "loadtest|python3 -m unittest discover -s tools/loadtest/tests"
  "jev-mock|python3 -m unittest discover -s tools/jev-mock/tests"
  "agent-runner|python3 -m unittest discover -s tools/agent-runner/tests"
  "wow-exporter|python3 -m unittest discover -s exporters/wow-exporter/tests"
  "wow-agents|python3 -m unittest discover -s scripts/wow-agents/tests"
  "scripts|python3 -m unittest discover -s scripts/tests"
  # needs tools/wowmap/requirements.txt + Pillow (the tools CI job installs them)
  "wowmap|python3 -m unittest discover -s tools/wowmap/tests"
  "wowmap-js|node --test tools/wowmap/tests/js/*.test.js"
  "worldknowledge|python3 -m unittest discover -s tools/worldknowledge/tests"
)
# Suites that must not need third-party packages (agent/ is stdlib-only).
STDLIB_SUITES="agent"

compile()   { find agent tools exporters scripts -name '*.py' -print0 | xargs -0 python3 -m py_compile; }
lint()      { ruff check .; }
# #410: tests are excluded from the mypy scope, except this one. Its _static_contract()
# assigns the real session and the shared fake to ports.ActionSession, so a port change
# the session or the fake does not follow is a type error.
CONTRACT_TESTS="agent/tests/test_ports.py"
types()     { mypy && mypy $CONTRACT_TESTS; }
types_legacy() { python3 scripts/mypy_legacy_baseline.py --check; }   # #378
generated() {
  local rc=0 f
  python3 scripts/generate.py --check || rc=1   # every generated file, #297
  for f in monitoring/grafana-dashboard-*.json; do
    python3 -m json.tool "$f" >/dev/null || { echo "invalid JSON: $f" >&2; rc=1; }
  done
  for f in scripts/wow-agents/*.sh; do bash -n "$f" || rc=1; done
  python3 scripts/gen_protocol_tables.py --check || rc=1   # opcode + update-field tables, #290
  return $rc
}
suite() {
  local s
  for s in "${SUITES[@]}"; do
    if [ "${s%%|*}" = "$1" ]; then
      echo "== test-$1"
      # #707: CHECK_STALL_DUMP_S set (CI only) = SIGABRT after that many seconds; with PYTHONFAULTHANDLER=1
      # python then dumps every thread's stack and dies. `exec` makes the test process timeout's direct child.
      if [ -n "${CHECK_STALL_DUMP_S:-}" ]; then
        timeout --signal=ABRT -k 10 "$CHECK_STALL_DUMP_S" bash -c "exec ${s#*|}"
      else
        bash -c "${s#*|}"
      fi
      return
    fi
  done
  echo "unknown suite: $1" >&2; return 2
}
test_all()  { local s; for s in "${SUITES[@]}"; do suite "${s%%|*}"; done; }
leftovers_in() { ls -A "$1"; }
# #725: every suite runs with a private TMPDIR under the system temp dir; anything it leaves there fails.
# Not in all (it runs every suite again): run it after touching a test's temp files.
tmp_leaks() (
  local base s n left rc=0
  base=$(mktemp -d "${TMPDIR:-/tmp}/check-tmp-leaks.XXXXXX")
  for s in "${SUITES[@]}"; do
    n=${s%%|*}
    mkdir "$base/$n"
    export TMPDIR="$base/$n"
    if ! suite "$n" > "$base/$n.log" 2>&1; then
      echo "suite $n failed:" >&2; tail -n 20 "$base/$n.log" >&2; rc=1
    fi
    left=$(leftovers_in "$base/$n")
    if [ -n "$left" ]; then
      echo "$n left behind in its TMPDIR:" >&2; sed 's/^/  /' <<< "$left" >&2; rc=1
    fi
  done
  rm -rf "$base"
  return $rc
)
# #624: every suite except the stdlib ones, which the agent CI job runs (test-stdlib).
test_tools() { local s n; for s in "${SUITES[@]}"; do n=${s%%|*}; case " $STDLIB_SUITES " in *" $n "*) ;; *) suite "$n" ;; esac; done; }

[ $# -gt 0 ] || set -- all
for t in "$@"; do
  case "$t" in
    compile|lint|generated) "$t" ;;
    types) types; types_legacy ;;   # CI's lint job runs this target (#378)
    types-legacy) types_legacy ;;
    test) test_all ;;
    test-stdlib) for s in $STDLIB_SUITES; do suite "$s"; done ;;
    test-tools) test_tools ;;   # must stay before test-*) (it would run a suite named "tools")
    test-*) suite "${t#test-}" ;;
    all) compile; lint; types; types_legacy; generated; test_all ;;
    coverage) python3 scripts/coverage_floor.py ;;   # #384, not in all: needs requirements-dev.txt
    list) for s in "${SUITES[@]}"; do echo "${s%%|*}"; done ;;
    tmp-leaks) tmp_leaks ;;   # #725, not in all
    *) echo "usage: $0 compile|lint|types|types-legacy|generated|test|test-<suite>|test-stdlib|test-tools|coverage|list|tmp-leaks|all" >&2; exit 2 ;;
  esac
done
