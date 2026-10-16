"""Fitness test (#678, ADR 0018 slice 2): every place the code writes a file or a SQLite
database has a row in INVENTORY with its class and version rule, and the ADR's inventory
table agrees with the non-exempt rows.

The scan is `ast` only (the scanned modules are never imported) over the tracked Python
files, test code excluded. A site is identified by (path, qualname).

Known misses (the scan does not see these): `Image.save`, `Path.open` with a variable mode,
`gzip.open`, `os.open`, `Path.touch` / `mkdir` / `rename`, `logging.FileHandler`, and shell
redirection or `mv` in the scripts.

Helpers: `persist` is the module `agent/persist.py` however it is imported (`from . import
persist`, `import agent.persist as ap`, `import agent.persist` and a dotted call). A local
variable that shadows an aliased module name is read as the module (a loud-side false positive).
A call to a private `_write_atomic` (the three local copies, #685) is a site, like
`persist.write_atomic`; the sites inside a function named `_write_atomic` are skipped, so the
helper bodies need no row and the callers are the rows.
"""
import ast
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADR = ROOT / "docs" / "adr" / "0018-persisted-state-versioning.md"
TOP_DIRS = ("agent", "tools", "scripts", "monitoring", "exporters")
CLASSES = ("cache", "document", "record-log", "sqlite", "binary-asset", "exempt")
FAIL_HINT = ("new persisted-state writer: add a row to INVENTORY in "
             "scripts/tests/test_persisted_state_inventory.py and, if it is not exempt, "
             "decide its class and version rule in docs/adr/0018 first")

Row = tuple[str, str, str, str, str]   # path, qualname, artefact, klass, version

_RENAMES = {"os.replace", "os.rename", "shutil.move", "shutil.copy", "shutil.copy2",
            "shutil.copyfile", "shutil.copyfileobj", "shutil.copytree"}
_PERSIST_WRITERS = {"persist.write_atomic", "persist.write_versioned"}   # agent/persist.py, the repo's helper
_STDLIB = ("os", "shutil", "sqlite3", "io")
_OPENERS = {"open": 1, "io.open": 1, "os.fdopen": 1}   # dotted name -> index of the mode argument


def _is_write_mode(node: ast.expr | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(c in node.value for c in "wax+")
    return True   # not a constant: cannot be proven read-only


def _mode_arg(call: ast.Call, index: int) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value
    return call.args[index] if len(call.args) > index else None


def _imports(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted name, for the stdlib modules and for the persist helper module."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname and a.name in _STDLIB:
                    out[a.asname] = a.name
                elif a.asname and a.name.rpartition(".")[2] == "persist":   # import agent.persist as ap
                    out[a.asname] = "persist"
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            for a in n.names:
                if mod in _STDLIB:
                    out[a.asname or a.name] = f"{mod}.{a.name}"
                elif mod.rpartition(".")[2] == "persist":
                    out[a.asname or a.name] = f"persist.{a.name}"
                elif a.name == "persist":   # from . import persist [as p]
                    out[a.asname or a.name] = "persist"
    return out


def _is_site(call: ast.Call, imported: dict[str, str]) -> bool:
    func = call.func
    name = ast.unparse(func)
    head, _, rest = name.partition(".")
    if head in imported:   # `from os import replace`, `import os as _os`
        name = imported[head] + ("." + rest if rest else "")
    if name.startswith("agent.persist."):   # import agent.persist; agent.persist.write_atomic(...)
        name = name[len("agent."):]
    if name in _RENAMES or name in _PERSIST_WRITERS or name == "_write_atomic":
        return True
    if name in _OPENERS:
        return _is_write_mode(_mode_arg(call, _OPENERS[name]))
    if name == "sqlite3.connect":
        first = call.args[0] if call.args else None
        read_only = (isinstance(first, ast.Constant) and isinstance(first.value, str)
                     and first.value.startswith("file:") and "mode=ro" in first.value)
        return not read_only
    if isinstance(func, ast.Attribute):
        if func.attr in ("write_text", "write_bytes"):
            return True
        # Path.open(mode): only a constant write mode counts. Image.open(p), realm.open(a, pw)
        # and WdbcFile.open(p) are not file writes. ponytail: a variable mode on a Path goes unseen.
        if func.attr == "open":
            mode = _mode_arg(call, 0)
            return isinstance(mode, ast.Constant) and _is_write_mode(mode)
    return False


def collect_sites(source: str, path: str) -> list[tuple[str, str, int]]:
    """Every write site of one source text as (path, qualname, line), sorted."""
    tree = ast.parse(source)
    imported = _imports(tree)
    sites: set[tuple[str, str, int]] = set()

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, scope + (child.name,))
                continue
            if isinstance(child, ast.Call) and scope[-1:] != ("_write_atomic",) and _is_site(child, imported):
                sites.add((path, ".".join(scope) or "<module>", child.lineno))
            walk(child, scope)

    walk(tree, ())
    return sorted(sites)


def tracked_python_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=ROOT, capture_output=True,
                             check=True).stdout.decode()
    except (OSError, subprocess.CalledProcessError) as e:
        raise unittest.SkipTest(f"git ls-files unavailable ({e}): the tracked file list is needed") from e
    files = []
    for f in sorted(filter(None, out.split("\0"))):
        parts = f.split("/")
        if parts[0] in TOP_DIRS and "tests" not in parts[:-1] and not parts[-1].startswith("test_"):
            files.append(f)
    return files


_sites_cache: list[tuple[str, str, int]] | None = None


def scan() -> list[tuple[str, str, int]]:
    global _sites_cache
    if _sites_cache is None:
        _sites_cache = [s for f in tracked_python_files()
                        for s in collect_sites((ROOT / f).read_text(encoding="utf-8"), f)]
    return _sites_cache


# Version rule of a non-exempt row: "none" (the ADR records it as unversioned; the PR lists it
# as a finding), or the NAME of a module-level constant in the row's own file, or
# "<repo path>:NAME" when the constant lives in another file; several, joined by ", ".
# An exempt row's `version` is the reason. `artefact` joins several names with "; ".
_PERSIST = "agent/persist.py"
_AUDIT_V = "agent/audit_schema.py:SCHEMA_VERSION"
INVENTORY: list[Row] = [
    ("agent/api_contract.py", "<module>", "docs/AGENT-API.md", "exempt", "generated, checked by --check"),
    ("agent/audit.py", "AuditLogger.record", "audit day file", "record-log", _AUDIT_V),
    ("agent/audit.py", "AuditLogger._rotate_if_needed", "audit day file", "record-log", _AUDIT_V),
    ("agent/audit.py", "AuditLogger.record_lifecycle", "lifecycle file", "record-log", "LIFECYCLE_SCHEMA"),
    ("agent/audit.py", "AuditLogger.record_unmet_intent", "unmet-intent file", "record-log", "agent/unmet.py:UNMET_SCHEMA"),
    ("agent/memory.py", "_connect", "memory.db", "sqlite", "MIGRATIONS"),
    ("agent/names.py", "NameCache.save", "names.json", "cache", "CACHE_SCHEMA"),
    ("agent/items.py", "ItemCache.save", "items.json", "cache", "CACHE_SCHEMA"),
    (_PERSIST, "write_atomic", "the helper itself", "exempt", "agent/persist.py: its callers are the rows"),
    (_PERSIST, "set_aside", "<cache>.corrupt", "exempt", "moved-aside copy of an unreadable file, never read"),
    ("agent/transport.py", "Transport._dump_packet", "raw packet dumps", "exempt",
     "AGENT_DUMP_PACKETS, off by default, read only by tools/agent_dev/dump_update.py"),
    ("agent/worldknowledge.py", "WorldKnowledge.__init__", "world bundle read-only connect", "exempt",
     "mode=ro, writes nothing"),
    ("monitoring/agent_metrics_textfile.py", "main", ".prom textfile", "exempt", "regenerated whole every run"),
    ("scripts/create_agent_roster.py", "save_roster", "roster.json", "document", "none"),
    ("scripts/gen_agents_compose.py", "main", "docker-compose.agents.yml", "exempt",
     "generated, checked by --check"),
    ("scripts/gen_protocol_tables.py", "refresh", "protocol excerpts", "exempt", "generated, regenerated whole"),
    ("scripts/gen_protocol_tables.py", "sync", "protocol tables block", "exempt", "generated, checked by --check"),
    ("scripts/genlib.py", "sync", "generated documents", "exempt", "generated, checked by --check"),
    ("scripts/llm_bench/benchmark.py", "main", "benchmark --json-out report", "exempt", "report for people"),
    ("scripts/mypy_legacy_baseline.py", "main", "mypy-legacy-baseline.json", "exempt",
     "generated, checked by --check"),
    ("scripts/mypy_legacy_baseline.py", "run_mypy", "temporary mypy config", "exempt", "temporary directory"),
    ("tools/agent-runner/fleet.py", "Fleet.save_agent", "state.json", "document", "none"),
    ("tools/agent-runner/fleet.py", "Fleet.write_compose", "generated compose file", "exempt",
     "rewritten whole on every run"),
    ("tools/agent_dev/ab.py", "run", "ab --record fixtures and --output artifact", "exempt",
     "dev tooling: a later run reads --record back as --fixture, but a person passes both paths"),
    ("tools/agent_dev/probe.py", "save_report", "probe report", "exempt",
     "ADR 0018 exempts it; REPORT_SCHEMA, exact match"),
    ("tools/fleet-limits/fleet_limits.py", "Writer.cycle", "fleet limits file", "exempt",
     "rewritten whole every period; version in the file, checked by agent/fleetlimits.py"),
    ("tools/loadtest/__main__.py", "main", "loadtest --out report", "exempt",
     "dev tooling: a later run reads it back as --compare, but a person passes the path"),
    ("tools/worldknowledge/build.py", "build", "world bundle", "binary-asset",
     "agent/worldknowledge.py:SCHEMA_VERSION"),
    ("tools/wowmap/pipeline/extract_models.py", "main", "model blob", "binary-asset",
     "tools/wowmap/wowmap/models.py:BLOB_VERSION"),
    ("tools/wowmap/wowmap/activity.py", "ActivityStore.__init__", "activity.sqlite3", "sqlite",
     "tools/wowmap/wowmap/activity_schema.py:MIGRATIONS"),
    ("tools/wowmap/wowmap/calibration.py", "save_calibration", "calibration.json", "document",
     "CALIBRATION_SCHEMA"),
]

# The ADR table's first cell (first backticked token) -> the artefact name used in INVENTORY.
# A new ADR row fails rule 4 until it is mapped here.
ADR_NAMES = {
    "~/.cache/wow-agent/names.json": "names.json",
    "~/.cache/wow-agent/items.json": "items.json",
    "AGENT_AUDIT_DIR/<agent>/<day>.jsonl[.N]": "audit day file",
    "AGENT_AUDIT_DIR/<agent>/lifecycle/<day>.jsonl": "lifecycle file",
    "AGENT_AUDIT_DIR/<agent>/unmet/<day>.jsonl": "unmet-intent file",
    "memory.db": "memory.db",
    "agents/roster.json": "roster.json",
    "<runtime>/state.json": "state.json",
    "calibration.json": "calibration.json",
    "ACTIVITY_DB": "activity.sqlite3",
    "MODELS_DIR/<race>_<gender>.bin": "model blob",
    "<runtime>/world/world.sqlite3": "world bundle",
}


def module_constants(source: str) -> set[str]:
    """Names bound at module level by assignment (an imported name is not a version constant)."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def missing_version_refs(path: str, version: str) -> list[str]:
    bad = []
    for ref in version.split(", "):
        target, _, name = ref.rpartition(":")
        file = ROOT / (target or path)
        if not file.is_file() or name not in module_constants(file.read_text(encoding="utf-8")):
            bad.append(ref)
    return bad


def adr_artefacts(text: str) -> set[str]:
    """First backticked token (else the first words) of each ADR inventory row whose class
    does not start with `exempt`."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("### Inventory"))
    found: set[str] = set()
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            if found:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == "Artefact" or cells[0].startswith("---") or cells[1].startswith("exempt"):
            continue
        token = re.search(r"`([^`]+)`", cells[0])
        found.add(token.group(1) if token else " ".join(cells[0].split()[:3]))
    return found


def inventory_artefacts() -> set[str]:
    return {a for _, _, art, klass, _ in INVENTORY if klass != "exempt" for a in art.split("; ")}


class PersistedStateInventoryTest(unittest.TestCase):
    def test_every_write_site_has_a_row(self):
        rows = {(p, q) for p, q, *_ in INVENTORY}
        bad = [f"{p}:{line} in {q}" for p, q, line in scan() if (p, q) not in rows]
        self.assertEqual(bad, [], FAIL_HINT)

    def test_every_row_has_a_write_site(self):
        sites = {(p, q) for p, q, _ in scan()}
        stale = [f"{p} {q}" for p, q, *_ in INVENTORY if (p, q) not in sites]
        self.assertEqual(stale, [], "inventory rows with no write site: delete them or fix the qualname")
        self.assertEqual(len({(p, q) for p, q, *_ in INVENTORY}), len(INVENTORY), "duplicate row")

    def test_rows_are_well_formed(self):
        bad = []
        for path, qualname, artefact, klass, version in INVENTORY:
            if klass not in CLASSES or not artefact or not version:
                bad.append(f"{path} {qualname}: class {klass!r}, artefact {artefact!r}, version {version!r}")
            elif klass != "exempt" and version != "none":
                bad += [f"{path} {qualname}: no module-level constant {ref!r}"
                        for ref in missing_version_refs(path, version)]
        self.assertEqual(bad, [])

    def test_adr_and_inventory_agree_on_non_exempt_artefacts(self):
        adr_rows = adr_artefacts(ADR.read_text(encoding="utf-8"))
        self.assertEqual(sorted(adr_rows - set(ADR_NAMES)), [], "ADR inventory row with no entry in ADR_NAMES")
        adr = {ADR_NAMES[a] for a in adr_rows}
        mine = inventory_artefacts()
        self.assertEqual(sorted(adr - mine), [], "in the ADR table, no non-exempt INVENTORY row")
        self.assertEqual(sorted(mine - adr), [], "in INVENTORY (non-exempt), no row in the ADR table")
        self.assertEqual(sorted(set(ADR_NAMES) - adr_rows), [], "ADR_NAMES entry for no ADR row")

    def test_unversioned_rows_are_unversioned_in_the_adr(self):
        lines = ADR.read_text(encoding="utf-8").splitlines()
        for path, qualname, artefact, klass, version in INVENTORY:
            if klass != "exempt" and version == "none":
                token = next(t for t, n in ADR_NAMES.items() if n == artefact)
                row = next(line for line in lines if line.startswith(f"| `{token}`"))
                self.assertTrue(row.split("|")[5].strip().startswith("none"),
                                f"{artefact} ({path} {qualname}): the ADR does not list it as unversioned")


class CollectorProbes(unittest.TestCase):
    def sites(self, src):
        return [(q, line) for _, q, line in collect_sites(src, "x.py")]

    def test_write_modes_are_sites(self):
        self.assertEqual(self.sites('def f(p):\n    open(p, "w")\n'), [("f", 2)])
        self.assertEqual(self.sites('def f(p):\n    open(p, mode="a")\n'), [("f", 2)])
        self.assertEqual(self.sites('def f(p):\n    open(p, "r+")\n'), [("f", 2)])

    def test_read_modes_are_not_sites(self):
        self.assertEqual(self.sites("def f(p):\n    open(p)\n"), [])
        self.assertEqual(self.sites('def f(p):\n    with open(p, "rb") as g:\n        g.read()\n'), [])

    def test_a_variable_mode_is_a_site(self):
        self.assertEqual(self.sites("def f(p, mode):\n    open(p, mode)\n"), [("f", 2)])

    def test_path_and_rename_calls(self):
        self.assertEqual(self.sites("def f(p, x):\n    Path(p).write_text(x)\n"), [("f", 2)])
        self.assertEqual(self.sites("def f(p, x):\n    p.write_bytes(x)\n"), [("f", 2)])
        self.assertEqual(self.sites("import os\ndef f(a, b):\n    os.replace(a, b)\n"), [("f", 3)])
        self.assertEqual(self.sites("from os import rename\ndef f(a, b):\n    rename(a, b)\n"), [("f", 3)])
        self.assertEqual(self.sites("import shutil\ndef f(a, b):\n    shutil.copy2(a, b)\n"), [("f", 3)])

    def test_path_open_only_counts_with_a_constant_write_mode(self):
        self.assertEqual(self.sites('def f(p):\n    p.open("w")\n'), [("f", 2)])
        self.assertEqual(self.sites("def f(p):\n    p.open()\n    Image.open(p)\n    realm.open(p, 'pw')\n"), [])

    def test_sqlite_connect(self):
        self.assertEqual(self.sites("import sqlite3\ndef f(p):\n    sqlite3.connect(p)\n"), [("f", 3)])
        self.assertEqual(self.sites("from sqlite3 import connect\ndef f(p):\n    connect(p)\n"), [("f", 3)])
        self.assertEqual(self.sites(
            'import sqlite3\ndef f():\n    sqlite3.connect("file:x?mode=ro", uri=True)\n'), [])

    def test_tempfile_alone_is_not_a_site(self):
        self.assertEqual(self.sites("import tempfile\ndef f():\n    tempfile.mkstemp()\n"), [])

    def test_qualnames(self):
        src = ("class A:\n    def m(self, p):\n        def inner():\n            open(p, 'w')\n"
               "        inner()\nopen('x', 'w')\n")
        self.assertEqual(self.sites(src), [("<module>", 6), ("A.m.inner", 4)])

    def test_module_aliases_are_resolved(self):
        self.assertEqual(self.sites("import sqlite3 as sq\ndef f(p):\n    sq.connect(p)\n"), [("f", 3)])
        self.assertEqual(self.sites("import os as _os\ndef f(a, b):\n    _os.replace(a, b)\n"), [("f", 3)])
        self.assertEqual(self.sites("import shutil as sh\ndef f(a, b):\n    sh.move(a, b)\n"), [("f", 3)])
        self.assertEqual(self.sites("from os import replace as rp\ndef f(a, b):\n    rp(a, b)\n"), [("f", 3)])

    def test_persist_helpers_are_sites(self):
        self.assertEqual(self.sites("from . import persist\ndef f(p):\n    persist.write_versioned(p, 1, {})\n"),
                         [("f", 3)])
        self.assertEqual(self.sites("from . import persist\ndef f(p):\n    persist.write_atomic(p, '')\n"),
                         [("f", 3)])
        self.assertEqual(self.sites("from .persist import write_atomic\ndef f(p):\n    write_atomic(p, '')\n"),
                         [("f", 3)])
        self.assertEqual(self.sites(
            "from .persist import write_versioned as wv\ndef f(p):\n    wv(p, 1, {})\n"), [("f", 3)])
        self.assertEqual(self.sites("from . import persist\ndef f(p):\n    persist.read_versioned(p, 1)\n"), [])

    def test_dotted_and_aliased_persist_imports(self):
        self.assertEqual(self.sites("import agent.persist as ap\ndef f(p):\n    ap.write_atomic(p, '')\n"),
                         [("f", 3)])
        self.assertEqual(self.sites("import agent.persist as ap\ndef f(p):\n    ap.write_versioned(p, 1, {})\n"),
                         [("f", 3)])
        self.assertEqual(self.sites("import agent.persist\ndef f(p):\n    agent.persist.write_atomic(p, '')\n"),
                         [("f", 3)])
        self.assertEqual(self.sites("import agent.persist as ap\ndef f(p):\n    ap.read_versioned(p, 1)\n"), [])
        self.assertEqual(self.sites("import agent.persist\ndef f(p):\n    agent.persist.read_versioned(p, 1)\n"), [])
        self.assertEqual(self.sites("import agent.other as ap\ndef f(p):\n    ap.write_atomic(p, '')\n"), [])

    def test_a_call_of_the_private_atomic_helper_is_a_site_its_body_is_not(self):
        src = ("import os\ndef _write_atomic(p, t):\n    f = os.fdopen(1, 'w')\n    os.replace('a', p)\n"
               "def save(p):\n    _write_atomic(p, '')\n")
        self.assertEqual(self.sites(src), [("save", 6)])

    def test_an_imported_name_is_not_a_version_constant(self):
        self.assertEqual(module_constants("from x import Callable\nA = 1\nB: int = 2\n"), {"A", "B"})

    def test_version_refs(self):
        self.assertEqual(missing_version_refs("agent/memory.py", "Callable"), ["Callable"])
        self.assertEqual(missing_version_refs("agent/names.py", "CACHE_SCHEMA"), [])
        self.assertEqual(missing_version_refs("agent/names.py", "NO_SUCH_CONSTANT"), ["NO_SUCH_CONSTANT"])
        self.assertEqual(missing_version_refs("agent/names.py", "agent/nope.py:X"), ["agent/nope.py:X"])

    def test_adr_table_parse_skips_exempt_rows(self):
        text = ("### Inventory (x)\n\n| Artefact | Class |\n|---|---|\n| `a.json` (z) | cache |\n"
                "| `b.log` | exempt, generated |\n| the probe report | x |\n\nafter\n")
        self.assertEqual(adr_artefacts(text), {"a.json", "the probe report"})


if __name__ == "__main__":
    unittest.main()
