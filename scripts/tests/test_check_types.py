"""Guard (#484, follow-up to #415): scripts/check.sh types() checks mypy.ini's scope, no module list.

mypy.ini (`files =` and the per-module sections) decides what is type-checked. A module list
in scripts/check.sh would drift from it, so types() runs bare mypy plus the one contract test
that sits outside mypy's scope (#410). The second check keeps a list from coming back under
any variable name.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT_PY = re.compile(r"agent/[\w/.*-]*\.py")


class CheckTypesTest(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "scripts" / "check.sh").read_text()

    def test_types_runs_bare_mypy_then_contract_tests(self):
        m = re.search(r"^types\(\)\s*\{(.*)\}\s*$", self.script, re.MULTILINE)
        self.assertIsNotNone(m, "types() one-line definition not found in scripts/check.sh")
        body = m.group(1).strip()
        if body.endswith(";"):
            body = body[:-1].rstrip()
        self.assertEqual(body, "mypy && mypy $CONTRACT_TESTS")

    def test_no_agent_module_list_outside_contract_tests(self):
        contract = re.search(r"^CONTRACT_TESTS=(.*)$", self.script, re.MULTILINE)
        self.assertIsNotNone(contract, "CONTRACT_TESTS assignment not found in scripts/check.sh")
        allowed = set(AGENT_PY.findall(contract.group(1)))
        found = set(AGENT_PY.findall(self.script))
        self.assertEqual(sorted(found - allowed), [], "agent/*.py module list in scripts/check.sh; mypy.ini owns the scope")


if __name__ == "__main__":
    unittest.main()
