"""Architecture fitness tests (#275): the layering rules of CLAUDE.md,
docs/ARCHITECTURE.md and ADRs 0005/0006/0008, enforced from the import graph.

Imports are read with `ast` (function-level imports included), so a lazy import
cannot hide an edge. LAYERS below is the one dependency table: each unit (a top
level module or package of `agent/`) lists the units it may import. Adding an edge
is a deliberate change to this table, in a PR that says why.
"""
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
TOOLS = ROOT / "tools"
STDLIB = sys.stdlib_module_names

# Units that are not layered: dev tools are run by hand, tests are not shipped.
UNLAYERED = {"tests"}

# unit -> units it may import. A unit missing here may import nothing from agent/.
# Groups (see docs/ARCHITECTURE.md, `agent/` table):
#   wire and leaf helpers: packets, opcodes, crypt, transport, config, handles, lines, action_names,
#       known_targets, metrics, api_contract, ports (the typed session contract, #304),
#       model (the snapshot contract, #288)
#   state files: persist (ADR 0018)
#   pure parsers and builders: npc, quests, loot, mail, trade, spells, channels, names,
#       items, item_compare, update_object, update_fields, movement
#   state: perception, state;  routing: router, handlers (ADR 0005, 0006)
#   acting: actions, reflexes, control, death, candidates;  deciding: jev, llm, deciders, brain, think
#   entry and observing: session, http_api, audit, login, supervisor, runloop, root, __main__
LAYERS = {
    "packets": set(),
    "wire": {"packets"},   # cursor Reader/Writer over packets' helpers, PacketTruncated is a ProtocolError (#274)
    "opcodes": set(),
    "crypt": set(),
    "changes": set(),   # the process-wide "perception changed" wake-up for action confirmation (#305)
    "transport": {"packets", "opcodes"},   # #793: the in-world desync check reads NUM_MSG_TYPES (Opcodes.h end of enum)
    "config": {"missions"},   # AGENT_MISSION is one of missions.MISSIONS (#314)
    "missions": set(),   # the closed mission set (#314); a leaf
    "handles": set(),
    "lines": set(),
    "known_targets": set(),
    "metrics": {"audit_schema", "opcodes", "transport"},   # opcode names for the router counters (#494); swallowed() reuses transport's log throttle (#306); reads audit lines (#277)
    "api_contract": {"action_specs"},   # renders the action catalog section of docs/AGENT-API.md (#504)
    "memory": {"metrics"},   # durable agent memory store (#276); sqlite3 only; metrics for swallowed() (#741)
    "model": set(),
    "audit_schema": {"model"},   # the audit record's versioned on-disk shape (#277)
    "auth": {"packets", "wire"},   # the logon challenge and realm list read through Reader (#517)
    "rules": set(),
    "ports": set(),
    "worldknowledge": set(),   # read-only world bundle reader (ADR 0016); stdlib sqlite3 only
    "persist": set(),   # atomic versioned JSON for the on-disk caches (ADR 0018); stdlib only
    "lifecycle": set(),   # connection lifecycle state machine (#584); stdlib only
    "unmet": set(),   # unmet-intent records (#319); stdlib only, no network, never an issue
    # parsers are pure: wire helpers only
    "npc": {"opcodes", "wire"},
    "quests": {"opcodes", "wire"},
    "loot": {"opcodes", "packets", "wire"},
    "mail": {"opcodes", "packets", "wire"},
    "trade": {"opcodes", "packets", "wire"},
    "spells": {"opcodes", "packets", "wire"},
    "channels": {"opcodes", "wire"},
    "names": {"opcodes", "persist", "wire"},
    "group": {"opcodes", "wire"},
    "items": {"persist"},
    "item_compare": set(),
    "update_object": {"opcodes", "packets", "wire"},
    "update_fields": {"update_object"},
    "movement": {"opcodes", "packets", "ports", "update_object"},   # #411: the session contract (ports)
    # state
    "perception": {"changes", "group", "handles", "items", "model", "names", "npc", "packets", "quests", "trade",
                   "update_fields", "update_object"},
    "state": {"changes", "perception"},
    # routing: handlers parse and update state, they never act (ADR 0006)
    "router": {"packets", "wire"},   # PacketTruncated is counted apart from other handler errors (#494)
    "handlers": {"channels", "group", "loot", "mail", "metrics", "names", "npc", "opcodes", "packets", "perception",
                 "quests", "router", "spells", "trade", "update_fields", "update_object", "wire"},
    # acting: actions never import reflexes (the follow reflex registers a hook instead)
    "actions": {"action_names", "action_specs", "changes", "channels", "item_compare", "loot", "mail", "movement", "npc", "opcodes", "ports",
                "quests", "rules", "spells", "trade", "update_fields"},
    "reflexes": {"action_names", "actions", "movement", "opcodes", "perception", "ports", "update_fields"},
    "control": {"actions", "metrics", "movement", "ports"},   # #411: the session contract (ports)
    "death": {"action_names", "actions", "movement", "opcodes", "perception", "ports", "rules"},   # #411: ports
    "action_specs": set(),   # one spec per action: name, params, required, cap (#419); below actions and candidates
    "action_names": {"action_specs"},
    "candidate_history": {"metrics", "model"},   # #550: the history drop/demote and the cap, moved out of candidates
    "candidates": {"action_names", "action_specs", "candidate_history", "handles", "item_compare", "missions", "model", "rules"},   # #550: metrics moved with the history code
    # deciding
    "prompts": set(),   # versioned prompt text assets (#395)
    "httpjson": set(),   # POST JSON over urllib, shared by jev and llm (#454)
    "tokenbudget": set(),   # the one AGENT_MAX_TOKENS_PER_HOUR implementation (#454)
    "llm": {"httpjson", "prompts"},
    "jev": {"httpjson", "llm"},
    "deciders": {"jev", "llm"},  # the Decider protocol and ScriptedDecider (#278)
    "fleetlimits": {"metrics"},   # the ADR 0017 limits file reader; metrics for swallowed()
    "brain": {"action_specs", "actions", "candidates", "fleetlimits", "jev", "llm", "metrics", "missions", "model", "tokenbudget"},  # #531: LLM_EXCLUDED_ACTIONS lives in action_specs
    "loopguard": {"config", "model", "watchdog"},     # think-loop history + loop guard, no session dependency (#385); config and watchdog: #320's ThinkState watchdog settings
    "watchdog": {"model"},   # #320: the idle watchdog and progress_fingerprint (the loop guard's "changed" test); no import of loopguard
    "think": {"action_specs", "actions", "brain", "handles", "llm", "loopguard", "metrics", "model", "ports", "spells", "trade", "unmet", "update_fields", "watchdog"},  # #411: ports; #319: unmet; #320: swallowed() and progress_fingerprint
    # entry and observing; nothing imports __main__
    "session": {"actions", "channels", "crypt", "handlers", "metrics", "opcodes", "packets", "router", "state",
                "transport", "wire"},   # enum_characters and handle_time_sync read through Reader (#517)
    "audit": {"audit_schema", "config", "metrics", "model"},
    "chat_relay": {"metrics"},
    "http_api": {"audit", "control", "metrics", "missions", "spells", "unmet", "update_fields"},   # #319: GET /unmet-intents
    "login": {"auth", "channels", "config", "death", "session"},   # auth, world connect, character login (#292); death: imported so its actions register (#321)
    "supervisor": {"auth", "lifecycle", "metrics"},   # the reconnect supervisor (#292)
    "runloop": {"metrics", "reflexes", "think"},   # reflex threads and the think loop of one session (#292)
    "root": {"audit", "brain", "chat_relay", "http_api", "lifecycle", "login", "metrics", "missions",
             "runloop", "spells", "supervisor", "unmet", "worldknowledge"},   # the composition root (#292): the one module that wires a whole agent
    "__main__": {"auth", "config", "root"},
}

ADR = {
    "handlers": "ADR 0005/0006 (handlers parse and update state, they do not act)",
    "actions": "docs/ARCHITECTURE.md (actions never import reflexes)",
}


def _imports(path: pathlib.Path, package: list[str]):
    """Yield (module, level) for every import in `path`, resolving relative imports
    against `package` (the dotted package of the file, e.g. ['agent', 'handlers'])."""
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)]
                if node.module:
                    yield ".".join(base + [node.module])
                else:
                    for alias in node.names:
                        yield ".".join(base + [alias.name])
            elif node.module == "agent":
                for alias in node.names:   # `from agent import actions` is an edge to agent.actions
                    yield "agent." + alias.name
            else:
                yield node.module


def _agent_files():
    for path in sorted(AGENT.rglob("*.py")):
        rel = path.relative_to(AGENT)
        if "__pycache__" in rel.parts:
            continue
        unit = rel.parts[0] if len(rel.parts) > 1 else rel.stem
        if unit == "__init__":
            continue
        package = ["agent"] + list(rel.parts[:-1])
        yield path, unit, package


def agent_graph():
    """unit -> {imported unit -> first file importing it}; plus third-party imports."""
    graph, third = {}, []
    for path, unit, package in _agent_files():
        if unit in UNLAYERED:
            continue
        edges = graph.setdefault(unit, {})
        for mod in _imports(path, package):
            top = mod.split(".")[0]
            if top == "agent":
                parts = mod.split(".")
                target = parts[1] if len(parts) > 1 else "__init__"
                if target != unit:
                    edges.setdefault(target, str(path.relative_to(ROOT)))
            elif top not in STDLIB:
                third.append((str(path.relative_to(ROOT)), mod))
    return graph, third


def _live_deque_loops(source: str, filename: str = "<src>") -> list[int]:
    """Line numbers where `source` reads `.events` or `.chat_inbox` other than as the direct
    argument of list(...) or len(...): a loop, a comprehension, reversed()/max()/map()/zip(),
    or an alias (`inbox = s.chat_inbox`) that is looped over later. A copy
    (`list(x.events)`, the snapshot methods) is the only safe read."""
    tree = ast.parse(source, filename)
    copies = {"list", "len"}
    safe = {id(n.args[0]) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") in copies and len(n.args) == 1}
    return sorted(n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute) and n.attr in {"events", "chat_inbox"}
                  and id(n) not in safe)


class AgentArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.third = agent_graph()

    def test_agent_is_stdlib_only(self):
        # CLAUDE.md: `agent/` is stdlib-only.
        third = [(f, m) for f, m in self.third if "/tests/" not in f]
        self.assertEqual(sorted(set(third)), [], "third-party import in agent/ (stdlib-only rule)")

    def test_every_unit_is_in_the_layer_table(self):
        missing = sorted(set(self.graph) - set(LAYERS))
        self.assertEqual(missing, [], "new agent/ module: add it to LAYERS with its allowed imports")
        stale = sorted(set(LAYERS) - set(self.graph))
        self.assertEqual(stale, [], "LAYERS lists a module that no longer exists")

    def test_imports_follow_the_layer_table(self):
        bad = []
        for unit, edges in self.graph.items():
            for target, where in edges.items():
                if target not in LAYERS.get(unit, set()):
                    why = ADR.get(unit, "docs/ARCHITECTURE.md, `agent/` table")
                    bad.append(f"{unit} -> {target} (in {where}); see {why}")
        self.assertEqual(bad, [], "import edge not allowed by LAYERS; add it only with a reason")

    def test_no_import_cycles_between_units(self):
        state, stack = {}, []

        def visit(unit):
            state[unit] = 1
            stack.append(unit)
            for target in self.graph.get(unit, {}):
                if state.get(target) == 1:
                    cycle = stack[stack.index(target):] + [target]
                    self.fail("import cycle: " + " -> ".join(cycle))
                if target not in state:
                    visit(target)
            stack.pop()
            state[unit] = 2

        for unit in sorted(self.graph):
            if unit not in state:
                visit(unit)

    def test_runtime_does_not_import_tools(self):
        # #397: tools/ (incl. tools/agent_dev) may import agent, never the reverse.
        bad = [f"{path.relative_to(ROOT)} imports {m}" for path, _, package in _agent_files()
               if "tests" not in path.relative_to(AGENT).parts
               for m in _imports(path, package) if m.split(".")[0] == "tools"]
        self.assertEqual(bad, [], "agent/ runtime must not import from tools/")

    def test_image_ships_no_tests_or_dev_tools(self):
        # #397: the image COPYs agent/; .dockerignore must keep tests out, dev CLIs live in tools/.
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn("agent/tests/", ignored)
        self.assertFalse((AGENT / "tools").exists(), "dev tools belong in tools/agent_dev")

    def test_brain_has_no_transport_code(self):
        # #454: Brain talks to a Decider; urllib and http live in agent/httpjson.py only.
        bad = [f"{path.relative_to(ROOT)} imports {m}" for path, unit, package in _agent_files()
               if unit.startswith("brain")
               for m in _imports(path, package) if m.split(".")[0] in {"urllib", "http"}]
        self.assertEqual(bad, [], "agent/brain* must not import urllib or http")

    def test_nothing_imports_main(self):
        offenders = [u for u, edges in self.graph.items() if "__main__" in edges]
        self.assertEqual(offenders, [])

    def test_broad_except_calls_swallowed(self):
        """#306: a broad `except Exception` in agent/ must call metrics.swallowed (log with
        context + count it), or re-raise. Narrow it
        instead if you can; a bare `except:` is never allowed."""
        bad = []
        for path, _, _ in _agent_files():
            rel = str(path.relative_to(ROOT))
            if "/tests/" in rel:
                continue
            for node in ast.walk(ast.parse(path.read_text(), rel)):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                names = {getattr(n, "id", "") for n in ast.walk(node.type)} if node.type else {"<bare>"}
                if not names & {"Exception", "BaseException", "<bare>"}:
                    continue
                calls = {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
                         for b in node.body for c in ast.walk(b) if isinstance(c, ast.Call)}
                reraises = any(isinstance(n, ast.Raise) for b in node.body for n in ast.walk(b))
                if "swallowed" not in calls and not reraises:
                    bad.append(f"{rel}:{node.lineno}")
        self.assertEqual(bad, [], "broad except without swallowed(where, log): see agent/metrics.py")

    def test_other_threads_never_iterate_the_live_deques(self):
        """#692: the recv thread (agent/state.py, agent/handlers/) is the only appender, so
        only it may loop over `events` / `chat_inbox`; everyone else takes events_snapshot()
        / chat_inbox_snapshot() (ADR 0009)."""
        bad = []
        for path, _, _ in _agent_files():
            rel = str(path.relative_to(ROOT))
            if "/tests/" in rel or rel == "agent/state.py" or rel.startswith("agent/handlers/"):
                continue
            bad += [f"{rel}:{n}" for n in _live_deque_loops(path.read_text(), rel)]
        self.assertEqual(bad, [], "iterate session.events_snapshot() / chat_inbox_snapshot(), not the deque")

    def test_the_live_deque_probe_catches_every_loop_shape(self):
        for src in ("for e in session.events: pass", "[e for e in s.chat_inbox]",
                    "any(e for e in s.events)", "for e in reversed(ctx.state.events): pass",
                    "for i, e in enumerate(s.events): pass", "inbox = session.chat_inbox",
                    "max(s.events, key=lambda e: e['t'])", "list(map(f, s.events))"):
            self.assertTrue(_live_deque_loops(src), src)
        for src in ("for e in list(s.events): pass", "for e in s.events_snapshot(): pass",
                    "for e in s.chat_inbox_snapshot(): pass", "n = len(s.events)"):
            self.assertEqual(_live_deque_loops(src), [], src)

    def test_parsers_do_not_reach_into_state_or_actions(self):
        # "parsers are pure": they may use the wire helpers and each other's data, never
        # session state, actions or the router.
        pure = {"npc", "quests", "loot", "mail", "trade", "spells", "channels", "names",
                "group", "items", "item_compare", "update_object", "update_fields"}
        forbidden = {"perception", "state", "actions", "handlers", "router", "session", "reflexes"}
        bad = [f"{u} -> {t}" for u in pure for t in self.graph.get(u, {}) if t in forbidden]
        self.assertEqual(bad, [])


def _tool_imports():
    for path in sorted(TOOLS.rglob("*.py")):
        rel = path.relative_to(TOOLS)
        if "__pycache__" in rel.parts or "tests" in rel.parts:
            continue
        yield rel.parts[0], path


class ToolsArchitectureTests(unittest.TestCase):
    def test_only_agent_runner_touches_docker_and_subprocess(self):
        # ADR 0002: tools/agent-runner owns the Docker socket; nothing else shells out to it.
        bad = []
        for tool, path in _tool_imports():
            if tool == "agent-runner":
                continue
            for mod in _imports(path, [tool]):
                if mod.split(".")[0] in {"docker", "subprocess"}:
                    bad.append(f"{path.relative_to(ROOT)} imports {mod}")
        self.assertEqual(bad, [])

    def test_tools_use_only_the_allowed_third_party_libraries(self):
        # CLAUDE.md: `tools/` may use pymysql/Pillow; mpyq is the MPQ reader of the
        # client-extraction scripts (extract_maps.py, extract_icons.py).
        allowed = {"pymysql", "PIL", "mpyq"}
        # First-party names: any module or package under tools/ or scripts/ (gen_agents_compose).
        local = {p.stem for p in list(TOOLS.rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))}
        local |= {d.name for d in TOOLS.rglob("*") if d.is_dir()}
        bad = []
        for tool, path in _tool_imports():
            for mod in _imports(path, [tool]):
                top = (mod or "").split(".")[0]
                if top and top not in STDLIB and top not in allowed and top not in local \
                        and top not in {"agent", "tools"}:
                    bad.append(f"{path.relative_to(ROOT)} imports {mod}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
