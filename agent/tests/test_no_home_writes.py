"""Guard (#433): the agent tests must never write under the real HOME."""

import os
import tempfile
import unittest

import agent.tests  # noqa: F401  (redirects the cache defaults)
from agent import items, names
from agent import perception as per


class NoHomeWritesTest(unittest.TestCase):
    def test_default_caches_live_in_a_temp_dir(self):
        ws = per.WorldState()
        for path in (ws.names.cache_path, ws.items.cache_path,
                     names.DEFAULT_CACHE_PATH, items.DEFAULT_CACHE_PATH):
            self.assertTrue(path.startswith(tempfile.gettempdir()), path)
            self.assertFalse(path.startswith(os.path.expanduser("~/.cache")), path)


if __name__ == "__main__":
    unittest.main()
