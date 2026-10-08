#!/usr/bin/env bash
# Single entry point shared by CI and contributors (#298).
#   scripts/check.sh <target>...   targets: compile lint types generated test test-<suite> all
# Add a test suite = add one line to SUITES. scripts/tests guards that no tests dir is missed.
set -euo pipefail
cd "$(dirname "$0")/.."

# name|command  (the command runs from the repo root)
SUITES=(
  "agent|python3 -m unittest discover -s agent/tests"
  "chat-feed|python3 -m unittest discover -s tools/chat-feed/tests"
  "dbc|python3 -m unittest discover -s tools/dbc/tests"
  "world-mock|python3 -m unittest discover -s tools/world-mock/tests"
  "jev-mock|python3 -m unittest discover -s tools/jev-mock/tests"
  "agent-runner|python3 -m unittest discover -s tools/agent-runner/tests"
  "wow-agents|python3 -m unittest discover -s scripts/wow-agents/tests"
  "scripts|python3 -m unittest discover -s scripts/tests"
  # needs tools/wowmap/requirements.txt + Pillow (the tools CI job installs them)
  "wowmap|python3 -m unittest discover -s tools/wowmap/tests"
  "wowmap-js|node --test tools/wowmap/tests/js/*.test.js"
)
# Suites that must not need third-party packages (agent/ is stdlib-only).
STDLIB_SUITES="agent"

compile()   { find agent tools exporters scripts -name '*.py' -print0 | xargs -0 python3 -m py_compile; }
lint()      { ruff check .; }
# #288: the snapshot-contract modules. Listed here, not in mypy.ini, because #375 is
# rewriting that file into tiers; drop this list once #375 checks them by default.
SNAPSHOT_TYPED="agent/model.py agent/candidates.py agent/brain.py agent/think.py agent/audit.py
  agent/perception/snapshot.py agent/perception/nearby.py agent/perception/inventory.py
  agent/perception/quest_log.py"
types()     { mypy && mypy $SNAPSHOT_TYPED; }
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
    if [ "${s%%|*}" = "$1" ]; then echo "== test-$1"; bash -c "${s#*|}"; return; fi
  done
  echo "unknown suite: $1" >&2; return 2
}
test_all()  { local s; for s in "${SUITES[@]}"; do suite "${s%%|*}"; done; }

[ $# -gt 0 ] || set -- all
for t in "$@"; do
  case "$t" in
    compile|lint|types|generated) "$t" ;;
    test) test_all ;;
    test-stdlib) for s in $STDLIB_SUITES; do suite "$s"; done ;;
    test-*) suite "${t#test-}" ;;
    all) compile; lint; types; generated; test_all ;;
    list) for s in "${SUITES[@]}"; do echo "${s%%|*}"; done ;;
    *) echo "usage: $0 compile|lint|types|generated|test|test-<suite>|test-stdlib|list|all" >&2; exit 2 ;;
  esac
done
