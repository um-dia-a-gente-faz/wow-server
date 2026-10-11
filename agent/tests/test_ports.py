"""The typed contract between actions/reflexes and the session (#304).

`agent.ports` names what actions and reflexes may use of a session. These tests
keep the real session and the shared test fake on that contract, and keep
actions/reflexes from reaching past it again.
"""
import ast
import pathlib
import unittest

from agent import ports, session, state
from agent import update_object as uo
from agent.tests import builders

AGENT = pathlib.Path(__file__).resolve().parents[1]
PORTS = (ports.PacketSink, ports.EventLog, ports.PlayerView, ports.Inbox, ports.ReflexSlots,
         ports.ActionSession)


def _sources(*names):
    """Parsed source of each package directory or single module (`movement` is agent/movement.py)."""
    for name in names:
        base = AGENT / name
        paths = sorted(base.rglob("*.py")) if base.is_dir() else [base.with_suffix(".py")]
        for path in paths:
            yield path, ast.parse(path.read_text(), str(path))


# The modules whose session access is typed: the packages #304 covered and the four #411 finished.
TYPED_SESSION_MODULES = ("actions", "reflexes", "movement", "death", "control", "think")


def _static_contract(real: session.WoWSession, fake: builders.FakeSession) -> None:
    """Never called. `scripts/check.sh types` checks this file (#410), so a port member
    the session or the fake lacks is a type error."""
    _real: ports.ActionSession = real
    _fake: ports.ActionSession = fake


def _update_object_return_types() -> None:
    """Never called (#517). Each assignment must fail to type-check, so its ignore is used: if the
    update_object decorator ever made a parser return Any again, mypy's warn_unused_ignores would
    report the ignore as unused and `scripts/check.sh types` would fail."""
    _blocks: int = uo.parse_update_object(b'')  # type: ignore[assignment]
    _info: str = uo.parse_movement_info(b'', 0)  # type: ignore[assignment]
    _move: int = uo.parse_monster_move(b'')  # type: ignore[assignment]


class PortConformanceTests(unittest.TestCase):
    def test_the_real_session_satisfies_every_port(self):
        sess = builders.make_session()
        for port in PORTS:
            self.assertIsInstance(sess, port, port.__name__)

    def test_the_shared_fake_satisfies_every_port(self):
        sess = builders.FakeSession()
        for port in PORTS:
            self.assertIsInstance(sess, port, port.__name__)

    def test_an_object_missing_a_member_is_not_a_port(self):
        self.assertNotIsInstance(object(), ports.PacketSink)

    def test_fake_records_sent_packets_and_events_like_the_session(self):
        sess = builders.FakeSession(player_guid=7)
        sess.send_packet(0x101, b"\x01")
        sess.record_event("resting_started", method="sit")
        self.assertEqual(sess.sent, [(0x101, b"\x01")])
        self.assertEqual(sess.player_guid, 7)
        self.assertEqual({k: v for k, v in sess.events[0].items() if k != "t"},
                         {"kind": "resting_started", "method": "sit"})
        self.assertIn("t", sess.events[0])


class SessionAccessTests(unittest.TestCase):
    """Acceptance of #304 and #411, enforced from the source of TYPED_SESSION_MODULES."""

    def test_no_getattr_on_the_session(self):
        bad = []
        for path, tree in _sources(*TYPED_SESSION_MODULES):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ("getattr", "setattr", "hasattr") and node.args
                        and isinstance(node.args[0], ast.Name) and node.args[0].id == "session"):
                    bad.append(f"{path.relative_to(AGENT)}:{node.lineno}")
        self.assertEqual(bad, [], "duck-typed session access; use a member of agent.ports")

    def test_no_private_session_member(self):
        bad = []
        for path, tree in _sources(*TYPED_SESSION_MODULES):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "session" and node.attr.startswith("_")):
                    bad.append(f"{path.relative_to(AGENT)}:{node.lineno} session.{node.attr}")
        self.assertEqual(bad, [], "private session member; add it to a port in agent.ports instead")

    def test_one_record_event_implementation(self):
        found = []
        for path in sorted(AGENT.rglob("*.py")):
            if "tests" in path.relative_to(AGENT).parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if isinstance(node, ast.FunctionDef) and node.name in ("record_event", "_record_event"):
                    found.append(f"{path.relative_to(AGENT)}:{node.name}")
        # ports.py declares the method on the EventLog protocol; state.py implements it.
        self.assertEqual(found, ["ports.py:record_event", "state.py:record_event"])
        self.assertIs(builders.FakeSession.record_event, state.GameState.record_event)
        self.assertFalse(hasattr(state.GameState, "_record_event"), "the alias is gone: call record_event")


if __name__ == "__main__":
    unittest.main()
