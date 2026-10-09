"""Test package. Importing it (every test does, via builders) points the name and
item caches' default path at a temp dir, so no test can write ~/.cache/wow-agent
(#433). The defaults are bound at def time, hence the __defaults__ swap."""

import atexit
import os
import shutil
import tempfile

from agent import items, names

_cache_dir = tempfile.mkdtemp(prefix="wow-agent-test-cache-")
atexit.register(shutil.rmtree, _cache_dir, ignore_errors=True)
for _mod, _cls, _file in ((names, names.NameCache, "names.json"), (items, items.ItemCache, "items.json")):
    _mod.DEFAULT_CACHE_PATH = os.path.join(_cache_dir, _file)
    _cls.__init__.__defaults__ = _cls.__init__.__defaults__[:-1] + (_mod.DEFAULT_CACHE_PATH,)
