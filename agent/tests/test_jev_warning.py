"""The Jev configured-but-not-selected warning fires even with no usable brain."""
import logging
import unittest
from types import SimpleNamespace

from agent.__main__ import _warn_jev_unselected


def _cfg(jev_enabled, agent_brain):
    return SimpleNamespace(jev_enabled=jev_enabled, agent_brain=agent_brain)


class JevUnselectedWarningTests(unittest.TestCase):
    def _run(self, cfg):
        with self.assertLogs("t", level="WARNING") as cm:
            logging.getLogger("t").warning("sentinel")
            _warn_jev_unselected(cfg, logging.getLogger("t"))
        return [m for m in cm.output if "Jev is configured" in m]

    def test_warns_when_llm_selected_regardless_of_llm_config(self):
        # No brain object is involved: the warning must not depend on one existing.
        self.assertEqual(len(self._run(_cfg(True, "llm"))), 1)

    def test_silent_when_jev_selected(self):
        self.assertEqual(self._run(_cfg(True, "jev")), [])

    def test_silent_when_jev_not_configured(self):
        self.assertEqual(self._run(_cfg(False, "llm")), [])


if __name__ == "__main__":
    unittest.main()
