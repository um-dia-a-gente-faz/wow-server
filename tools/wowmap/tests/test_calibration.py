"""#608: save_calibration never overwrites a calibration.json it cannot read.

A missing file is an empty store (saves create it). A present file that is not a valid store
(bad JSON, not an object, a zone that is not an object or whose offsets do not parse) is
unreadable: loading warns and returns {}, saving raises CalibrationUnreadable and leaves the
bytes alone. Every save re-reads the file, so fixing it takes effect without a restart."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

WOWMAP = pathlib.Path(__file__).resolve().parents[1] / "wowmap"
TOOLS = WOWMAP.parents[1]
sys.path.insert(0, str(WOWMAP))
import calibration  # noqa: E402
import routes  # noqa: E402
import state  # noqa: E402
from webio import Request  # noqa: E402

GOOD = {"100": {"dx": 1.5, "dy": -2.0}, "200": {"dx": 0.0, "dy": 3.25}}


class CalibrationFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "calibration.json")
        for p in (mock.patch.object(calibration, "CALIBRATION_FILE", self.path),
                  mock.patch.object(calibration, "calibrations", {})):
            p.start()
            self.addCleanup(p.stop)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def raw(self):
        with open(self.path, "rb") as f:
            return f.read()

    def start_up(self):
        """What the module does at import: fill the shared store from the file."""
        calibration.calibrations.update(calibration.load_calibrations())

    def assert_refused(self, area_id=300):
        before = self.raw()
        with self.assertRaises(calibration.CalibrationUnreadable):
            calibration.save_calibration(area_id, 1.0, 2.0)
        self.assertEqual(self.raw(), before)

    def test_malformed_json_refuses_the_save_and_keeps_the_bytes(self):
        self.write('{"100": {"dx": 1.5, "dy": -2.0},')
        with self.assertLogs(state.log, "WARNING"):
            self.start_up()
        self.assert_refused()

    def test_a_non_numeric_offset_refuses_the_save_and_keeps_the_bytes(self):
        self.write(json.dumps({"100": {"dx": "abc", "dy": 0}, "200": {"dx": 1, "dy": 1}}))
        with self.assertLogs(state.log, "WARNING"):
            self.start_up()
        self.assertEqual(calibration.calibrations, {})
        self.assert_refused()

    def test_a_null_offset_does_not_raise_on_import_and_refuses_the_save(self):
        self.write(json.dumps({"100": {"dx": None, "dy": 0}, "200": {"dx": 1, "dy": 1}}))
        before = self.raw()
        env = {"PYTHONPATH": f"{WOWMAP}:{TOOLS}", "CALIBRATION_FILE": self.path}
        r = subprocess.run([sys.executable, "-c", "import calibration; print(calibration.calibrations)"],
                           cwd=WOWMAP, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "{}")
        self.assertIn(self.path, r.stderr)
        self.assertEqual(self.raw(), before)
        self.assert_refused()

    def test_a_non_object_zone_refuses_the_save_and_keeps_the_bytes(self):
        self.write(json.dumps({"100": [1, 2], "200": {"dx": 1, "dy": 1}}))
        with self.assertLogs(state.log, "WARNING"):
            self.start_up()
        self.assert_refused()

    def test_a_top_level_value_that_is_not_an_object_refuses_the_save(self):
        self.write("[1, 2, 3]")
        with self.assertLogs(state.log, "WARNING"):
            self.start_up()
        self.assert_refused()

    def test_unreadable_store_loads_empty_with_one_warning_naming_the_path(self):
        self.write('{"100": {"dx": "abc"}}')
        with self.assertLogs(state.log, "WARNING") as logs:
            self.assertEqual(calibration.load_calibrations(), {})
        self.assertEqual(len(logs.output), 1)
        self.assertIn(self.path, logs.output[0])

    def test_a_missing_file_is_an_empty_store_and_the_save_creates_it(self):
        self.assertFalse(os.path.exists(self.path))
        self.start_up()
        self.assertEqual(calibration.calibrations, {})
        self.assertEqual(calibration.save_calibration(100, 1.234, -2), {"dx": 1.23, "dy": -2.0})
        self.assertEqual(json.loads(self.raw()), {"100": {"dx": 1.23, "dy": -2.0}})

    def test_a_valid_file_keeps_the_other_zones_after_a_save(self):
        self.write(json.dumps(GOOD))
        self.start_up()
        calibration.save_calibration(300, 4, 5)
        self.assertEqual(json.loads(self.raw()), {
            "100": {"dx": 1.5, "dy": -2.0},
            "200": {"dx": 0.0, "dy": 3.25},
            "300": {"dx": 4.0, "dy": 5.0},
        })

    def test_fixing_the_file_lifts_the_refusal_without_a_restart(self):
        self.write('{"100": {"dx": 1.5, "dy": -2.0},')
        with self.assertLogs(state.log, "WARNING"):
            self.start_up()
        self.assert_refused()
        self.write(json.dumps(GOOD))
        calibration.save_calibration(300, 4, 5)
        self.assertEqual(sorted(json.loads(self.raw())), ["100", "200", "300"])

    def test_the_route_answers_500_with_the_message_and_keeps_the_file(self):
        self.write('{"100": {"dx": 1.5, "dy": -2.0},')
        before = self.raw()
        body = b'{"area_id": 300, "dx": 1, "dy": 2}'
        req = Request("POST", "/api/calibrate", {}, {"Content-Length": str(len(body))},
                      lambda n: body[:n], "10.0.0.9")
        with self.assertLogs(state.log, "WARNING"):
            status, payload, ctype, _ = routes.dispatch(req)
        self.assertEqual((status, ctype), (500, "application/json"))
        self.assertIn("unreadable", payload["error"])
        self.assertIn(self.path, payload["error"])
        self.assertEqual(self.raw(), before)


if __name__ == "__main__":
    unittest.main()
