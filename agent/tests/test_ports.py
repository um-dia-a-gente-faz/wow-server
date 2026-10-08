"""The typed contract between actions/reflexes and the session (#304).

`agent.ports` names what actions and reflexes may use of a session. These tests
keep the real session and the shared test fake on that contract, and keep
actions/reflexes from reaching past it again.
"""
import ast
import pathlib
import unittest

from agent import ports, session, state
from agent.tests import builders

AGENT = pathlib.Path(__file__).resolve().parents[1]
PORTS = (ports.PacketSink, ports.EventLog, ports.PlayerView, ports.Inbox, ports.ReflexSlots,
         ports.ActionSession)


def _sources(*packages):
    for package in packages:
        for path in sorted((AGENT / package).rglob("*.py")):
            yield path, ast.parse(path.read_text(), str(path))


def _static_contract(real: session.WoWSession, fake: builders.FakeSession) -> None:
    """Never called. mypy checks these assignments, so a port member the session or
    the fake lacks is a type error instead of an AttributeError at run time."""
    _real: ports.ActionSession = real
    _fake: ports.ActionSession = fake


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
    """Acceptance of #304, enforced from the source of agent/actions and agent/reflexes."""

    def test_no_getattr_on_the_session(self):
        bad = []
        for path, tree in _sources("actions", "reflexes"):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ("getattr", "setattr", "hasattr") and node.args
                        and isinstance(node.args[0], ast.Name) and node.args[0].id == "session"):
                    bad.append(f"{path.relative_to(AGENT)}:{node.lineno}")
        self.assertEqual(bad, [], "duck-typed session access; use a member of agent.ports")

    def test_no_private_session_member(self):
        bad = []
        for path, tree in _sources("actions", "reflexes"):
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


if __name__ == "__main__":
    unittest.main()
