"""Layering fitness tests for tools/ (#387): the tools counterpart of agent/tests/test_architecture.py.

Static imports are read with `ast` (function-level imports, try blocks and `if TYPE_CHECKING:`
included): every `import` and `from ... import` is an edge. Dynamic loading is checked narrower.
The check finds every reference to five loader names (`__import__`, `import_module`,
`spec_from_file_location`, `run_path`, `run_module`) and pins, per file, the files that have one in
DYNAMIC_IMPORTS. It does not see `getattr`, `exec` or loader classes such as `SourceFileLoader`,
and it does not look at what a pinned file loads: a pin is a reviewed decision, not a proof.
The tool modules are never imported: the graph comes from source, and this suite stays stdlib-only.
Each table below lists exactly what may be imported today. A new edge, or a pinned edge the code no
longer has, is a deliberate edit to its table, in a PR that says why.
"""
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
STDLIB = sys.stdlib_module_names
THIRD_PARTY = {"pymysql", "PIL", "mpyq"}   # what tools/ may import from outside (CLAUDE.md)
TOOL_DIRS = {d.name for d in TOOLS.iterdir() if d.is_dir() and d.name not in {"__pycache__", "tests"}}

# Layered trees: a layer is a directory whose modules are units (wowmap/wowmap/repo/* is one unit).
LAYER_DIRS = {
    "wowmap": "tools/wowmap/wowmap",
    "wowmap/pipeline": "tools/wowmap/pipeline",
    "agent-runner": "tools/agent-runner",
}

# sys.path roots, as each entry point or importer sets them. A bare name `import x` resolves to a
# tool file only when that file is under one of these roots, so wowmap's fleet.py is not
# agent-runner's fleet.py, and a name no root holds is an unresolved import, not a silent miss.
ROOTS = {
    "agent-runner": ["tools/agent-runner", "tools", "."],         # characters.py: tools/; "." is the checkout root, for agents/ (#391)
    "chat-feed": ["tools/chat-feed", "tools"],                    # app.py appends tools/
    "common": ["tools/common", "tools"],                          # imported by path from tools/ (wowmap, chat-feed, agent-runner)
    "dbc": ["tools/dbc", "tools"],                                # imported by path from tools/ (wowmap)
    "loadtest": ["tools/loadtest", "tools/world-mock"],           # soak.py inserts the repo root and world-mock/
    "wowmap": ["tools/wowmap/wowmap", "tools"],                   # state.py appends tools/
    "wowmap/pipeline": ["tools/wowmap/pipeline", "tools/wowmap/wowmap"],   # extract_*.py insert ../wowmap
}

# Rule 1, between packages: tool directory -> the other tools/ packages it may import.
PACKAGE_EDGES = {
    "agent-runner": {"common"},          # characters.py: common.agent_accounts
    "agent_dev": set(),
    "chat-feed": {"common"},             # app.py: common.env, common.jsonserver
    "common": set(),
    "dbc": set(),
    "jev-mock": set(),
    "loadtest": {"world-mock"},          # soak.py: `import server` from tools/world-mock (not in the #387 list; pinned, see PR)
    "wowmap": {"common", "dbc"},         # state.py, app.py, character.py
    "world-mock": set(),
}

# Rule 2, into agent/: tool -> the agent modules (agent/<unit>) it may import. Serving tools import
# none; the dev and test tools are pinned to exactly what they import today.
AGENT_EDGES = {
    "agent-runner": set(),
    "agent_dev": {"actions", "audit_schema", "candidates", "config", "jev", "known_targets", "lines",
                  "llm", "login", "packets", "reflexes", "rules", "update_fields", "update_object"},
    "chat-feed": set(),
    "common": set(),
    "dbc": set(),
    "jev-mock": set(),
    "loadtest": {"handles", "items", "login", "names", "npc", "quests", "runloop", "supervisor"},
    "wowmap": set(),
    "world-mock": {"auth", "crypt", "opcodes", "packets", "quests", "update_fields", "update_object"},
}

# Rule 2: files that import agent.tests from outside a tests/ directory. #349 removes this.
AGENT_TESTS_IMPORTERS = {
    "tools/world-mock/death_scenario.py",
    "tools/world-mock/server.py",
}

# Rule 3, inside wowmap: each module of wowmap/wowmap/ (repo/ is one unit) -> the units it may import.
WOWMAP_LAYERS = {
    "activity": set(),
    "agents": {"fleet", "state", "walk", "webio"},
    "app": {"activity", "routes", "state", "webio"},
    "areas": {"calibration", "fog", "overlays", "repo", "state", "transform"},
    "assets": {"fogview", "state", "webio"},
    "calibration": {"state"},
    "character": {"agents", "inventory", "players", "repo", "state"},
    "fleet": set(),
    "fog": set(),
    "fogview": {"fog", "repo", "state"},
    "inventory": {"item_tooltip", "repo", "state"},
    "item_icons": set(),
    "item_tooltip": set(),
    "models": set(),
    "overlays": set(),
    "pages": {"state"},
    "players": {"repo", "state"},
    "repo": {"item_tooltip", "state"},
    "routes": {"agents", "areas", "assets", "calibration", "character", "fogview", "pages", "players", "state",
               "webio"},
    "state": {"item_icons", "models", "overlays", "transform"},
    "transform": set(),
    "walk": set(),
    "webio": set(),
}

# Rule 4, inside agent-runner: each module of tools/agent-runner/ -> the units it may import.
RUNNER_LAYERS = {
    "characters": set(),
    "core": set(),
    "docker_backend": {"core"},
    "fleet": {"characters", "core", "docker_backend"},
    "handler": {"core"},
    "runner": {"core", "docker_backend", "fleet", "handler"},
}

# Rule 5: wowmap's offline pipeline/ -> the wowmap runtime modules it may import. wowmap never imports it.
PIPELINE_RUNTIME_IMPORTS = {"item_icons", "models", "overlays", "transform"}

# Dynamic imports: files with a reference to one of DYNAMIC_CALLS (a loader that loads a module by
# name at run time). A static read cannot follow what they load, so each such file is pinned here
# with its reason. The check does not see getattr, exec or loader classes, and does not check what a
# pinned file loads. Today: none (#391 removed characters.py's load_script).
DYNAMIC_IMPORTS: set[str] = set()
DYNAMIC_CALLS = {"__import__", "import_module", "spec_from_file_location", "run_path", "run_module"}


def _files():
    """(repo-relative posix path, path) of every non-test .py under tools/."""
    for path in sorted(TOOLS.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "__pycache__" in rel.split("/") or "tests" in rel.split("/"):
            continue
        yield rel, path


def _layer(rel):
    """The layered tree a path is in ('wowmap', 'wowmap/pipeline', 'agent-runner'), or ''."""
    for name, prefix in LAYER_DIRS.items():
        if rel.startswith(prefix + "/"):
            return name
    return ""


def _unit(rel):
    """The unit a file belongs to inside a layered tree, '' elsewhere."""
    layer = _layer(rel)
    if not layer:
        return ""
    return rel[len(LAYER_DIRS[layer]) + 1:].split("/")[0].removesuffix(".py")


def _find(base, parts):
    """The file or package `parts` names under `base`, as the import system finds it; None if absent."""
    here = base
    for i, part in enumerate(parts):
        if i == len(parts) - 1 and (here / f"{part}.py").is_file():
            return here / f"{part}.py"
        if not (here / part).is_dir():
            return None
        here = here / part
    init = here / "__init__.py"
    return init if init.is_file() else here


def _resolve(rel, line, name, level, exact=False):
    """One import, classified: 'agent' (a unit of agent/), 'tool' (a file under tools/), 'external'
    (stdlib, an allowed library, or a file outside tools/) or 'unresolved' (none of those).
    With `exact`, return None unless the name itself names a tool file, package or agent unit."""
    tool = rel.split("/")[1]
    layer = _layer(rel)
    edge = {"src": rel, "tool": tool, "layer": layer, "unit": _unit(rel), "where": f"{rel}:{line}",
            "name": "." * level + name, "roots": ROOTS.get(layer or tool, [f"tools/{tool}"])}
    parts = name.split(".") if name else []
    if level == 0 and parts[:1] == ["agent"]:
        return {**edge, "kind": "agent", "unit": parts[1] if len(parts) > 1 else "__init__"}
    if level == 0 and parts[:1] == ["tools"] and len(parts) > 1:
        if (TOOLS / parts[1]).is_dir():
            return {**edge, "kind": "tool", "dst_tool": parts[1], "dst_layer": "", "dst_unit": ""}
        return None if exact else {**edge, "kind": "unresolved"}
    if level:
        base = (ROOT / rel).parent
        for _ in range(level - 1):
            base = base.parent
        found = _find(base, parts)
    else:
        found = None
        for root in edge["roots"]:
            found = _find(ROOT / root, parts)
            if found is not None:
                break
    if found is None:
        if exact:
            return None
        top = parts[0] if parts else ""
        kind = "external" if not level and (top in STDLIB or top in THIRD_PARTY) else "unresolved"
        return {**edge, "kind": kind}
    target = found.relative_to(ROOT).as_posix()
    if target == "tools":   # a path inside tools/ is never external; the tools/ directory is no module
        return None if exact else {**edge, "kind": "unresolved"}
    if not target.startswith("tools/"):
        # Outside tools/ only the checkout root reaches (agent-runner's "."), and through it only the
        # agents/ library modules (#391): scripts/, docs/ and the other top-level dirs stay unresolved,
        # and so does a package __init__ (`from agents import <missing>` falls back to the package).
        module = pathlib.PurePosixPath(target)
        if module.parent.as_posix() == "agents" and module.suffix == ".py" and module.stem != "__init__":
            return {**edge, "kind": "external"}
        return {**edge, "kind": "unresolved"}
    return {**edge, "kind": "tool", "dst_tool": target.split("/")[1], "dst_layer": _layer(target),
            "dst_unit": _unit(target)}


def _edges_from(rel, source):
    """Every import in one file's source, function-level and guarded ones included. `from X import y`
    is the edge to `X.y` when that names a file, package or agent unit, else the edge to `X`."""
    out = []
    for node in ast.walk(ast.parse(source, rel)):
        if isinstance(node, ast.Import):
            out += [_resolve(rel, node.lineno, alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                edge = _resolve(rel, node.lineno, full, node.level, exact=True)
                out.append(edge or _resolve(rel, node.lineno, module, node.level))
    return out


def tool_edges():
    out = []
    for rel, path in _files():
        out += _edges_from(rel, path.read_text())
    return out


def _dynamic_sites_from(rel, source):
    """(where, name) for every reference to one of DYNAMIC_CALLS in one file's source: a name or an
    attribute carrying that name (a call, or a binding such as `imp = __import__`), and a
    `from ... import` of it (possibly aliased). Each reference is one AST node, so a call is counted once."""
    out = []
    for node in ast.walk(ast.parse(source, rel)):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in DYNAMIC_CALLS:
                    out.append((f"{rel}:{node.lineno}", alias.name))
            continue
        else:
            continue
        if name in DYNAMIC_CALLS:
            out.append((f"{rel}:{node.lineno}", name))
    return out


def dynamic_sites():
    out = []
    for rel, path in _files():
        out += _dynamic_sites_from(rel, path.read_text())
    return out


def _scripts_path_sites_from(rel, source):
    """where (file:line) of every string literal in one file's source that names a scripts/
    directory as a path: exactly "scripts", or ending in "/scripts". That is how a tool would
    put the checkout's scripts/ on sys.path or load a file from it (#391)."""
    return [f"{rel}:{node.lineno}" for node in ast.walk(ast.parse(source, rel))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and (node.value == "scripts" or node.value.endswith("/scripts"))]


def scripts_path_sites():
    out = []
    for rel, path in _files():
        out += _scripts_path_sites_from(rel, path.read_text())
    return out


def scripts_root_problems(roots):
    """roots: a ROOTS-shaped table. A tool with scripts/ as an import root imports scripts by path."""
    return [f"{tool}: ROOTS lists {root!r}; a tool must not import from scripts/ (#391), move the code "
            f"into the agents/ library" for tool in sorted(roots) for root in roots[tool]
            if root == "scripts" or root.startswith("scripts/")]


def package_graph(edges):
    graph = {}
    for e in edges:
        if e["kind"] == "tool" and e["dst_tool"] != e["tool"]:
            graph.setdefault(e["tool"], {}).setdefault(e["dst_tool"], e["where"])
    return graph


def agent_graph(edges):
    graph = {}
    for e in edges:
        if e["kind"] == "agent" and e["unit"] != "tests":
            graph.setdefault(e["tool"], {}).setdefault(e["unit"], e["where"])
    return graph


def agent_test_importers(edges):
    return {e["src"] for e in edges if e["kind"] == "agent" and e["unit"] == "tests"}


def layer_graph(edges, layer):
    """unit -> unit inside one layered tree. A target in another layer is named 'layer:unit'."""
    graph = {}
    for e in edges:
        if e["kind"] != "tool" or e["layer"] != layer or e["dst_tool"] != e["tool"]:
            continue
        same = e["dst_layer"] == layer
        dst = e["dst_unit"] if same else f"{e['dst_layer']}:{e['dst_unit']}"
        if same and dst == e["unit"]:
            continue
        graph.setdefault(e["unit"], {}).setdefault(dst, e["where"])
    return graph


def pipeline_runtime_graph(edges):
    graph = {}
    for e in edges:
        if e["kind"] == "tool" and e["layer"] == "wowmap/pipeline" and e["dst_layer"] == "wowmap":
            graph.setdefault("pipeline", {}).setdefault(e["dst_unit"], e["where"])
    return graph


def _units(layer):
    return {_unit(rel) for rel, _ in _files() if _layer(rel) == layer}


# ---- the checkers: pure functions of a graph, so the self-checks can feed them synthetic ones

def edge_problems(graph, table, table_name):
    return [f"{src} -> {dst} (in {graph[src][dst]}): not allowed by {table_name}; add it there with a reason"
            for src in sorted(graph) for dst in sorted(graph[src]) if dst not in table.get(src, set())]


def pin_problems(graph, table, table_name):
    return [f"{src} -> {dst}: pinned in {table_name} but no longer imported; remove the pin"
            for src in sorted(table) for dst in sorted(table[src] - set(graph.get(src, {})))]


def coverage_problems(units, table, table_name):
    return ([f"{u}: not in {table_name}; add it with its allowed imports" for u in sorted(set(units) - set(table))]
            + [f"{u}: {table_name} lists it but it no longer exists" for u in sorted(set(table) - set(units))])


def cycle_problems(graph, known=frozenset()):
    found, state, stack = [], {}, []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for target in sorted(graph.get(node, {})):
            if state.get(target) == 1:
                cycle = " -> ".join(stack[stack.index(target):] + [target])
                if cycle not in known:
                    found.append(f"import cycle: {cycle}")
            elif target not in state:
                visit(target)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if node not in state:
            visit(node)
    return found


def unresolved_problems(edges):
    return [f"{e['where']} imports {e['name']}: no file under its roots {e['roots']}. If that is a real "
            f"sys.path root of this tool, add it to ROOTS; if it is a library, add it to THIRD_PARTY "
            f"(CLAUDE.md allows only pymysql, PIL, mpyq); otherwise the import is wrong"
            for e in edges if e["kind"] == "unresolved"]


def check_packages(graph, tools, table=PACKAGE_EDGES):
    return (coverage_problems(tools, table, "PACKAGE_EDGES") + edge_problems(graph, table, "PACKAGE_EDGES")
            + pin_problems(graph, table, "PACKAGE_EDGES"))


def check_agent(graph, tools, table=AGENT_EDGES):
    return (coverage_problems(tools, table, "AGENT_EDGES") + edge_problems(graph, table, "AGENT_EDGES")
            + pin_problems(graph, table, "AGENT_EDGES"))


def check_agent_tests(importers, pinned=AGENT_TESTS_IMPORTERS):
    return ([f"{f}: imports agent.tests outside a tests/ directory; pin it in AGENT_TESTS_IMPORTERS (#349 removes it)"
             for f in sorted(importers - pinned)]
            + [f"{f}: pinned in AGENT_TESTS_IMPORTERS but no longer imports agent.tests" for f in sorted(pinned - importers)])


def check_layers(graph, units, table, table_name, known=frozenset()):
    return (coverage_problems(units, table, table_name) + edge_problems(graph, table, table_name)
            + pin_problems(graph, table, table_name) + cycle_problems(graph, known))


def check_dynamic(sites, pinned):
    """sites: (where, call) pairs; pinned: the files DYNAMIC_IMPORTS allows."""
    seen = {where.split(":")[0] for where, _ in sites}
    return ([f"{where}: {call}() loads a module by name, which no static rule can follow; "
             f"pin the file in DYNAMIC_IMPORTS with a reason, or make the import static"
             for where, call in sorted(sites) if where.split(":")[0] not in pinned]
            + [f"{f}: pinned in DYNAMIC_IMPORTS but has no dynamic import any more; remove the pin"
               for f in sorted(pinned - seen)])


def check_pinned(graph, table, table_name):
    return edge_problems(graph, table, table_name) + pin_problems(graph, table, table_name)


class ToolsArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edges = tool_edges()

    def test_every_import_resolves_on_its_path(self):
        self.assertEqual(unresolved_problems(self.edges), [], "import that resolves to nothing on its path")

    def test_dynamic_imports_are_pinned(self):
        # reviewer fix 2: __import__ and importlib/runpy load by name at run time; no static edge shows it.
        self.assertEqual(check_dynamic(dynamic_sites(), DYNAMIC_IMPORTS), [],
                         "dynamic import outside DYNAMIC_IMPORTS")

    def test_no_tool_imports_from_scripts(self):
        # #391: a service importing a repo script by file path. Static imports resolve only through
        # ROOTS, dynamic loaders are pinned above, and a scripts/ path literal is how either is reached.
        self.assertEqual(scripts_root_problems(ROOTS), [], "scripts/ as an import root")
        self.assertEqual(scripts_path_sites(), [], "a tools/ file names the scripts/ directory as a path")

    def test_the_scripts_rule_reports_a_root_and_a_path_literal(self):
        self.assertEqual(len(scripts_root_problems({"x": ["tools/x", "scripts"]})), 1)
        self.assertEqual(scripts_root_problems({"x": ["tools/x", "tools", "."]}), [])
        src = 'import sys\nsys.path.insert(0, str(repo / "scripts"))\nx = "see scripts/gen.py"\n'
        self.assertEqual(_scripts_path_sites_from("tools/x/a.py", src), ["tools/x/a.py:2"])
        self.assertEqual(_scripts_path_sites_from("tools/x/a.py", 'p = os.path.join(root, "../scripts")\n'),
                         ["tools/x/a.py:1"])

    def test_packages_import_only_the_allowed_tools(self):
        self.assertEqual(check_packages(package_graph(self.edges), TOOL_DIRS), [],
                         "tool-to-tool import not in PACKAGE_EDGES (shared code goes in tools/common)")

    def test_serving_and_dev_tools_import_only_pinned_agent_modules(self):
        self.assertEqual(check_agent(agent_graph(self.edges), TOOL_DIRS), [],
                         "tools/ imports an agent/ module not pinned in AGENT_EDGES")

    def test_agent_tests_are_imported_only_from_pinned_files(self):
        self.assertEqual(check_agent_tests(agent_test_importers(self.edges)), [],
                         "agent.tests imported outside AGENT_TESTS_IMPORTERS")

    def test_wowmap_modules_follow_wowmap_layers(self):
        self.assertEqual(check_layers(layer_graph(self.edges, "wowmap"), _units("wowmap"), WOWMAP_LAYERS,
                                      "WOWMAP_LAYERS"), [],
                         "wowmap/wowmap import edge not in WOWMAP_LAYERS (wowmap never imports pipeline)")

    def test_agent_runner_modules_follow_runner_layers(self):
        self.assertEqual(check_layers(layer_graph(self.edges, "agent-runner"), _units("agent-runner"),
                                      RUNNER_LAYERS, "RUNNER_LAYERS"), [],
                         "agent-runner import edge not in RUNNER_LAYERS")

    def test_pipeline_imports_only_the_pinned_runtime_modules(self):
        self.assertEqual(check_pinned(pipeline_runtime_graph(self.edges), {"pipeline": PIPELINE_RUNTIME_IMPORTS},
                                      "PIPELINE_RUNTIME_IMPORTS"), [],
                         "pipeline/ imports a wowmap runtime module not pinned in PIPELINE_RUNTIME_IMPORTS")


class ResolutionSelfChecks(unittest.TestCase):
    def test_a_lazy_or_guarded_import_is_an_edge(self):
        src = ("def f():\n    import fleet\n"
               "try:\n    from agent import brain\nexcept ImportError:\n    pass\n"
               "if TYPE_CHECKING:\n    import state\n")
        names = sorted((e["kind"], e["name"]) for e in _edges_from("tools/wowmap/wowmap/app.py", src))
        self.assertEqual(names, [("agent", "agent.brain"), ("tool", "fleet"), ("tool", "state")])

    def test_a_name_resolves_to_a_tool_file_only_on_the_importers_path(self):
        kinds = {rel: _resolve(rel, 1, "fleet", 0)["kind"] for rel in (
            "tools/wowmap/wowmap/agents.py", "tools/agent-runner/runner.py", "tools/chat-feed/app.py")}
        self.assertEqual(kinds, {"tools/wowmap/wowmap/agents.py": "tool",
                                 "tools/agent-runner/runner.py": "tool",
                                 "tools/chat-feed/app.py": "unresolved"})

    def test_from_dotted_import_of_a_tool_package_names_that_tool(self):
        # reviewer fix 1: `from .. import common` named no tool before, so the edge was lost.
        edges = _edges_from("tools/agent_dev/replay.py", "from .. import common\n")
        self.assertEqual([(e["kind"], e.get("dst_tool")) for e in edges], [("tool", "common")])

    def test_from_package_import_of_a_submodule_names_the_submodule(self):
        # `from wowmap.pipeline import extract_maps` names the file, not the package it sits in.
        edges = _edges_from("tools/wowmap/wowmap/app.py", "from wowmap.pipeline import extract_maps\n")
        self.assertEqual([(e["kind"], e.get("dst_layer"), e.get("dst_unit")) for e in edges],
                         [("tool", "wowmap/pipeline", "extract_maps")])

    def test_common_and_dbc_resolve_each_other_through_the_tools_root(self):
        # reviewer fix 3: tools/ is on sys.path whenever common or dbc is importable.
        self.assertEqual(_resolve("tools/common/env.py", 1, "dbc", 0)["dst_tool"], "dbc")
        self.assertEqual(_resolve("tools/dbc/wdbc.py", 1, "common", 0)["dst_tool"], "common")

    def test_an_unresolved_import_message_names_the_tables_to_edit(self):
        msgs = unresolved_problems([{"where": "tools/chat-feed/app.py:9", "name": "fleet", "kind": "unresolved",
                                     "roots": ["tools/chat-feed", "tools"]}])
        self.assertEqual(len(msgs), 1)
        self.assertIn("ROOTS", msgs[0])
        self.assertIn("THIRD_PARTY", msgs[0])

    def test_a_relative_from_import_that_names_nothing_is_unresolved_never_external(self):
        # a path inside tools/ is never "external"; `from .. import nothere` must not pass silently.
        edges = _edges_from("tools/agent_dev/replay.py", "from .. import nothere\n")
        self.assertEqual([e["kind"] for e in edges], ["unresolved"])

    # #391: the checkout root is on agent-runner's path for agents/ only. Each name below is a
    # directory at the checkout root, so before the narrowing the runner resolved it as external.
    @staticmethod
    def _runner_kinds(src):
        return [e["kind"] for e in _edges_from("tools/agent-runner/runner.py", src)]

    def test_the_runner_may_not_import_scripts_from_the_root(self):
        self.assertEqual(self._runner_kinds("import scripts\n"), ["unresolved"])

    def test_the_runner_may_not_import_exporters_from_the_root(self):
        self.assertEqual(self._runner_kinds("import exporters\n"), ["unresolved"])

    def test_the_runner_may_not_import_monitoring_from_the_root(self):
        self.assertEqual(self._runner_kinds("import monitoring\n"), ["unresolved"])

    def test_the_runner_may_not_import_docs_from_the_root(self):
        self.assertEqual(self._runner_kinds("import docs\n"), ["unresolved"])

    def test_the_runner_may_not_import_dev_from_the_root(self):
        self.assertEqual(self._runner_kinds("import dev\n"), ["unresolved"])

    def test_the_runner_may_not_import_tdb_from_the_root(self):
        self.assertEqual(self._runner_kinds("import tdb\n"), ["unresolved"])

    def test_the_runner_may_not_import_grafana_from_the_root(self):
        self.assertEqual(self._runner_kinds("import grafana\n"), ["unresolved"])

    def test_the_runner_may_not_import_a_missing_agents_module(self):
        self.assertEqual(self._runner_kinds("from agents import nothere\n"), ["unresolved"])

    def test_the_runner_may_import_an_existing_agents_module_from_the_root(self):
        self.assertEqual(self._runner_kinds("from agents import roster, compose\n"), ["external", "external"])


class CheckerSelfChecks(unittest.TestCase):
    """Each rule's checker must report a synthetic graph that breaks it."""

    def test_rule1_reports_a_tool_edge_not_in_the_table(self):
        msgs = check_packages({"chat-feed": {"wowmap": "tools/chat-feed/app.py:9"}},
                              {"chat-feed", "wowmap"}, {"chat-feed": {"common"}, "wowmap": set()})
        self.assertTrue(any(m.startswith("chat-feed -> wowmap (in tools/chat-feed/app.py:9)") for m in msgs), msgs)

    def test_rule1_reports_a_pinned_edge_the_graph_lost(self):
        msgs = check_packages({}, {"chat-feed", "common"}, {"chat-feed": {"common"}, "common": set()})
        self.assertTrue(any("chat-feed -> common: pinned" in m for m in msgs), msgs)

    def test_rule2_reports_an_agent_module_from_a_serving_tool(self):
        msgs = check_agent({"wowmap": {"brain": "tools/wowmap/wowmap/app.py:3"}}, {"wowmap"}, {"wowmap": set()})
        self.assertTrue(any(m.startswith("wowmap -> brain") for m in msgs), msgs)

    def test_rule2_reports_agent_tests_imported_from_an_unpinned_file(self):
        msgs = check_agent_tests({"tools/x/y.py"}, set())
        self.assertTrue(any(m.startswith("tools/x/y.py: imports agent.tests") for m in msgs), msgs)

    def test_rules3_to_4_report_an_edge_outside_the_table(self):
        msgs = check_layers({"webio": {"app": "tools/wowmap/wowmap/webio.py:1"}}, {"webio", "app"},
                            {"webio": set(), "app": set()}, "WOWMAP_LAYERS")
        self.assertTrue(any(m.startswith("webio -> app (in tools/wowmap/wowmap/webio.py:1)") for m in msgs), msgs)

    def test_rules3_to_4_report_a_cycle(self):
        graph = {"app": {"webio": "a:1"}, "webio": {"app": "b:1"}}
        msgs = check_layers(graph, {"app", "webio"}, {"app": {"webio"}, "webio": {"app"}}, "WOWMAP_LAYERS")
        self.assertEqual(msgs, ["import cycle: app -> webio -> app"])

    def test_rules3_to_4_report_a_module_missing_from_the_table(self):
        msgs = check_layers({}, {"new_module"}, {}, "RUNNER_LAYERS")
        self.assertTrue(any(m.startswith("new_module: not in RUNNER_LAYERS") for m in msgs), msgs)

    def test_dynamic_import_references_are_found(self):
        src = ("import importlib, runpy\n"
               "importlib.import_module('agent.brain')\n"
               "__import__('dbc')\n"
               "importlib.util.spec_from_file_location('m', 'p')\n"
               "runpy.run_path('p')\n"
               "runpy.run_module('m')\n"
               "from importlib import import_module\n")   # a reference too: the from-import is counted
        calls = sorted(call for _, call in _dynamic_sites_from("tools/wowmap/wowmap/app.py", src))
        self.assertEqual(calls, ["__import__", "import_module", "import_module", "run_module", "run_path",
                                 "spec_from_file_location"])

    def test_an_aliased_from_import_of_a_dynamic_function_is_a_reference(self):
        # reviewer round 2, fix 1: `from importlib import import_module as im` then `im('fleet')`.
        sites = _dynamic_sites_from("tools/chat-feed/app.py",
                                    "from importlib import import_module as im\nim('fleet')\n")
        self.assertEqual(sites, [("tools/chat-feed/app.py:1", "import_module")])

    def test_a_dynamic_function_bound_to_a_name_is_a_reference(self):
        # reviewer round 2, fix 1: `imp = __import__` then `imp('dbc')`.
        sites = _dynamic_sites_from("tools/chat-feed/app.py", "imp = __import__\nimp('dbc')\n")
        self.assertEqual(sites, [("tools/chat-feed/app.py:1", "__import__")])

    def test_a_dynamic_import_outside_the_table_names_the_table(self):
        msgs = check_dynamic([("tools/wowmap/wowmap/app.py:9", "import_module")], set())
        self.assertTrue(any(m.startswith("tools/wowmap/wowmap/app.py:9: import_module") and
                            "DYNAMIC_IMPORTS" in m for m in msgs), msgs)

    def test_a_pinned_file_with_no_dynamic_import_is_reported(self):
        msgs = check_dynamic([], {"tools/agent-runner/characters.py"})
        self.assertTrue(any("characters.py" in m and "DYNAMIC_IMPORTS" in m for m in msgs), msgs)

    def test_rule5_reports_an_unpinned_runtime_module(self):
        msgs = check_pinned({"pipeline": {"app": "tools/wowmap/pipeline/x.py:2"}},
                            {"pipeline": {"transform"}}, "PIPELINE_RUNTIME_IMPORTS")
        self.assertTrue(any(m.startswith("pipeline -> app (in tools/wowmap/pipeline/x.py:2)") for m in msgs), msgs)


if __name__ == "__main__":
    unittest.main()
