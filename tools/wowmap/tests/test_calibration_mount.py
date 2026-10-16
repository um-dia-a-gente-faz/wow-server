"""#698: the wowmap container mounts a directory for calibration, never calibration.json itself.

save_calibration writes a temporary file beside CALIBRATION_FILE and os.replace()s it over the
target. A single-file bind mount pins the old inode, so the rename fails with EBUSY. The compose
service therefore mounts /opt/wowmap-data at /app/data and CALIBRATION_FILE lives under it. The
compose file is read as text (stdlib only, no YAML parser in the wowmap CI job)."""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

import _calibration_env  # noqa: F401 (#670: before any wowmap import; the subprocesses below set their own)

ROOT = pathlib.Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "monitoring" / "docker-compose.yml"
WOWMAP = ROOT / "tools" / "wowmap" / "wowmap"
TOOLS = WOWMAP.parents[1]


def wowmap_service(text):
    """The lines of the `wowmap:` service block, from the line after its key to the next key at
    the same level (two spaces or less of indentation)."""
    lines = text.splitlines()
    start = lines.index("  wowmap:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^( {0,2})\S", lines[i])), len(lines))
    return lines[start + 1:end]


def section(block, key):
    """The `- item` entries under the four-space `key:` of the service block, comments skipped."""
    items, inside = [], False
    for line in block:
        if re.match(r"^    \S", line):
            inside = line.strip() == f"{key}:"
        elif inside and line.startswith("      - "):
            items.append(line[len("      - "):].strip().strip('"'))
    return items


class WowmapComposeMountTests(unittest.TestCase):
    def setUp(self):
        self.block = wowmap_service(COMPOSE.read_text(encoding="utf-8"))
        self.volumes = section(self.block, "volumes")
        self.env = section(self.block, "environment")

    def test_the_wowmap_service_mounts_the_data_directory_at_app_data(self):
        self.assertIn("/opt/wowmap-data:/app/data", self.volumes)

    def test_calibration_file_is_under_the_mounted_directory(self):
        self.assertIn("CALIBRATION_FILE=/app/data/calibration.json", self.env)

    def test_no_volume_target_is_the_calibration_file(self):
        targets = [v.split(":")[1] for v in self.volumes if v.count(":") >= 1]
        self.assertFalse([t for t in targets if t.endswith("calibration.json")], targets)


class CalibrationRoundTripTests(unittest.TestCase):
    """A save in one process is loaded by the next, with CALIBRATION_FILE in a temporary
    directory that does not exist yet (save creates it). Existing coverage is partial:
    test_calibration.py::CalibrationFileTests.test_a_valid_file_keeps_the_other_zones_after_a_save
    checks the saved bytes but never loads them back in a new process."""

    def run_python(self, code, path):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{WOWMAP}:{TOOLS}",
               "CALIBRATION_FILE": path}
        return subprocess.run([sys.executable, "-c", code], cwd=WOWMAP, capture_output=True,
                              text=True, env=env)

    def test_a_save_round_trips_through_a_load_under_a_temp_directory(self):
        with tempfile.TemporaryDirectory(prefix="wowmap-698-") as tmp:
            path = os.path.join(tmp, "data", "calibration.json")
            saved = self.run_python("import calibration; calibration.save_calibration(7, 1.5, -2)", path)
            self.assertEqual(saved.returncode, 0, saved.stderr)
            loaded = self.run_python(
                "import calibration, json; print(json.dumps(calibration.load_calibrations(), sort_keys=True))",
                path)
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertEqual(json.loads(loaded.stdout), {"7": {"dx": 1.5, "dy": -2.0}})
            # the temporary is gone: only the target is left in the directory
            self.assertEqual(os.listdir(os.path.dirname(path)), ["calibration.json"])


if __name__ == "__main__":
    unittest.main()
