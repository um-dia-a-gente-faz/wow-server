"""Point calibration.json at a temporary file for this test process (#670).

calibration.py loads its store at import from $CALIBRATION_FILE, or else from the tracked
wowmap/calibration.json next to it. Importing this module before any wowmap module keeps the
tracked file out of the test run. The temporary file starts absent, which is an empty store.
A CALIBRATION_FILE that is already set is kept: a developer or CI may point it somewhere on purpose.
"""
import atexit
import os
import tempfile

if "CALIBRATION_FILE" not in os.environ:
    _directory = tempfile.TemporaryDirectory(prefix="wowmap-tests-")
    atexit.register(_directory.cleanup)
    os.environ["CALIBRATION_FILE"] = os.path.join(_directory.name, "calibration.json")
