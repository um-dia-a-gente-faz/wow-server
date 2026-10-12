"""Pins the audit keys of the two raw readers to the AuditRecord fields (#635, ADR 0018).

`audit_event` (tools/wowmap/wowmap/activity.py) and `Fleet.audit` (tools/agent-runner/fleet.py)
read audit lines as plain dicts, not through AuditRecord. The log changes by addition only, so a
key these readers use must stay a field of AuditRecord: a key that is not one is either a typo or
a field that was renamed or removed without the readers following. The modules are parsed with
`ast`, never imported, so this suite stays stdlib-only and does not depend on what the readers
import. A reader that is renamed or moved fails the test instead of passing with nothing collected.
"""
import ast
import dataclasses
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.audit_schema import AuditRecord  # noqa: E402

FIELD_NAMES = {f.name for f in dataclasses.fields(AuditRecord)} - {"extra"}
ACTIVITY = ROOT / "tools" / "wowmap" / "wowmap" / "activity.py"
FLEET = ROOT / "tools" / "agent-runner" / "fleet.py"


def _find(body, name, where):
    for node in body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{where}: function {name!r} not found; update this test with the reader")


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _reader_keys(source, func, var, cls=None, where="source"):
    """Constant string keys read from `var` inside `func` (a module function, or a method of
    `cls`): `var.get("k")` and `var["k"]`."""
    tree = ast.parse(source)
    body = tree.body
    if cls is not None:
        body = _find_class(tree, cls, where).body
    node = _find(body, func, where)
    keys = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript) and _is_name(sub.value, var):
            key = _const_str(sub.slice)
        elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
              and sub.func.attr == "get" and _is_name(sub.func.value, var) and sub.args):
            key = _const_str(sub.args[0])
        else:
            continue
        if key is not None:
            keys.add(key)
    return keys


def _find_class(tree, name, where):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{where}: class {name!r} not found; update this test with the reader")


def _is_name(node, name):
    return isinstance(node, ast.Name) and node.id == name


def _unknown(keys):
    return keys - FIELD_NAMES


class ReaderKeysTest(unittest.TestCase):
    def setUp(self):
        self.audit_event_keys = _reader_keys(
            ACTIVITY.read_text(encoding="utf-8"), "audit_event", "rec", where="activity.py")
        self.fleet_audit_keys = _reader_keys(
            FLEET.read_text(encoding="utf-8"), "audit", "record", cls="Fleet", where="fleet.py")

    def test_audit_event_collects_keys_including_ts(self):
        self.assertIn("ts", self.audit_event_keys)
        self.assertGreater(len(self.audit_event_keys), 1)

    def test_fleet_audit_collects_keys_including_ts(self):
        self.assertIn("ts", self.fleet_audit_keys)
        self.assertGreater(len(self.fleet_audit_keys), 1)

    def test_every_key_audit_event_reads_is_an_audit_record_field(self):
        self.assertEqual(_unknown(self.audit_event_keys), set(),
                         f"audit_event reads keys that are not AuditRecord fields: "
                         f"{sorted(self.audit_event_keys)}")

    def test_every_key_fleet_audit_reads_is_an_audit_record_field(self):
        self.assertEqual(_unknown(self.fleet_audit_keys), set(),
                         f"Fleet.audit reads keys that are not AuditRecord fields: "
                         f"{sorted(self.fleet_audit_keys)}")

    def test_an_unknown_key_in_a_reader_is_reported(self):
        # The same collector and the same check, on an in-memory reader that reads one key
        # AuditRecord does not have: the check must name it.
        source = (
            "def audit_event(rec, agent):\n"
            "    ts = rec['ts']\n"
            "    return rec.get('bogus_key')\n"
        )
        keys = _reader_keys(source, "audit_event", "rec")
        self.assertEqual(keys, {"ts", "bogus_key"})
        self.assertEqual(_unknown(keys), {"bogus_key"})

    def test_a_missing_reader_fails_loudly(self):
        with self.assertRaisesRegex(AssertionError, "not found"):
            _reader_keys("def other(rec):\n    return rec.get('ts')\n", "audit_event", "rec")


if __name__ == "__main__":
    unittest.main()
