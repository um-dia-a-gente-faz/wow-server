"""#608: save_calibration never overwrites a calibration.json it cannot read.

A missing file is an empty store (saves create it). A present file that is not a valid store
(bad JSON, not an object, a zone that is not an object or whose offsets do not parse) is
unreadable: loading warns and returns {}, saving raises CalibrationUnreadable and leaves the
bytes alone. Every save re-reads the file, so fixing it takes effect without a restart."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
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
        expected = {
            "100": {"dx": 1.5, "dy": -2.0},
            "200": {"dx": 0.0, "dy": 3.25},
            "300": {"dx": 4, "dy": 5},  # round(4, 2) is the int 4, written as such
        }
        self.assertEqual(json.loads(self.raw()), expected)
        # the bytes are the ones the save has always written: indent 2, sorted keys, final newline
        self.assertEqual(self.raw(), (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode())

    def test_a_load_of_a_good_file_leaves_its_bytes_identical(self):
        for text in (json.dumps(GOOD),
                     json.dumps({"schema": 1, **GOOD}),
                     json.dumps({"schema": 1, **GOOD}, indent=4)):
            with self.subTest(text=text):
                self.write(text)
                before = self.raw()
                self.assertEqual(calibration.load_calibrations(), {"100": {"dx": 1.5, "dy": -2.0},
                                                                   "200": {"dx": 0.0, "dy": 3.25}})
                self.assertEqual(calibration._read_store(), {"100": {"dx": 1.5, "dy": -2.0},
                                                             "200": {"dx": 0.0, "dy": 3.25}})
                self.assertEqual(self.raw(), before)

    def test_the_reserved_schema_refusal_comes_before_the_read(self):
        # an unreadable file: the refusal must be the plain ValueError, not the read's CalibrationUnreadable
        self.write('{"100": ')
        before = self.raw()
        with self.assertRaises(ValueError) as ctx:
            calibration.save_calibration("schema", 1, 2)
        self.assertNotIsInstance(ctx.exception, calibration.CalibrationUnreadable)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        # no file at all: the refusal creates nothing and loads nothing
        os.remove(self.path)
        with self.assertRaises(ValueError) as ctx:
            calibration.save_calibration("schema", 1, 2)
        self.assertNotIsInstance(ctx.exception, calibration.CalibrationUnreadable)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(calibration.calibrations, {})

    def test_save_refuses_the_reserved_schema_zone_before_touching_the_file(self):
        self.write(json.dumps(GOOD))
        before = self.raw()
        with self.assertRaises(ValueError) as ctx:
            calibration.save_calibration("schema", 1, 2)
        self.assertNotIsInstance(ctx.exception, calibration.CalibrationUnreadable)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_the_route_answers_400_for_the_reserved_schema_zone(self):
        self.write(json.dumps(GOOD))
        before = self.raw()
        body = b'{"area_id": "schema", "dx": 1, "dy": 2}'
        req = Request("POST", "/api/calibrate", {}, {"Content-Length": str(len(body))},
                      lambda n: body[:n], "10.0.0.9")
        status, payload = calibration.calibrate(req)
        self.assertEqual(status, 400)
        self.assertEqual(self.raw(), before)

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

    def import_in_subprocess(self):
        env = {"PYTHONPATH": f"{WOWMAP}:{TOOLS}", "CALIBRATION_FILE": self.path}
        return subprocess.run([sys.executable, "-c", "import calibration"],
                              cwd=WOWMAP, capture_output=True, text=True, env=env)

    def assert_unreadable_shape(self):
        """The loader contract for an unreadable file: import exits 0, load gives {} with one
        warning naming the path, and a save is refused with the bytes kept."""
        before = self.raw()
        r = self.import_in_subprocess()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.path, r.stderr)
        with self.assertLogs(state.log, "WARNING") as logs:
            self.assertEqual(calibration.load_calibrations(), {})
        self.assertEqual(len(logs.output), 1)
        self.assertIn(self.path, logs.output[0])
        self.assertEqual(self.raw(), before)
        self.assert_refused()

    def test_a_400_digit_integer_offset_is_unreadable_not_a_crash(self):
        self.write('{"100": {"dx": ' + "1" * 400 + ', "dy": 0}, "200": {"dx": 1, "dy": 1}}')
        self.assert_unreadable_shape()

    def test_a_200000_deep_nested_file_is_unreadable_not_a_crash(self):
        depth = 200_000
        self.write('{"100": {"dx": 0, "dy": 0, "extra": ' + "[" * depth + "]" * depth + "}}")
        self.assert_unreadable_shape()

    def test_a_zero_byte_file_is_an_empty_store_and_a_save_writes_it(self):
        self.write("")
        with self.assertNoLogs(state.log, "WARNING"):
            self.assertEqual(calibration.load_calibrations(), {})
        self.assertEqual(calibration.save_calibration(100, 1, 2), {"dx": 1.0, "dy": 2.0})
        self.assertEqual(json.loads(self.raw()), {"100": {"dx": 1.0, "dy": 2.0}})

    def test_a_whitespace_only_file_is_an_empty_store_and_a_save_writes_it(self):
        self.write("  \n")
        with self.assertNoLogs(state.log, "WARNING"):
            self.assertEqual(calibration.load_calibrations(), {})
        self.assertEqual(calibration.save_calibration(100, 1, 2), {"dx": 1.0, "dy": 2.0})
        self.assertEqual(json.loads(self.raw()), {"100": {"dx": 1.0, "dy": 2.0}})

    def test_a_save_refreshes_the_shared_store_in_place_and_never_clears_it(self):
        class NoClearDict(dict):
            def clear(self):
                raise AssertionError("the shared store must be refreshed, not emptied first")

        store = NoClearDict({"100": {"dx": 0.0, "dy": 0.0}, "500": {"dx": 9.0, "dy": 9.0}})
        self.write(json.dumps({"100": {"dx": 1.5, "dy": -2.0}, "200": {"dx": 0.0, "dy": 3.25},
                               "300": {"dx": 7.0, "dy": 8.0}}))   # 300 is only in the file
        with mock.patch.object(calibration, "calibrations", store):
            shared = calibration.calibrations
            calibration.save_calibration(400, 4, 5)
            self.assertIs(calibration.calibrations, shared)
        self.assertEqual(store, json.loads(self.raw()))
        self.assertEqual(sorted(store), ["100", "200", "300", "400"])  # 500 was only in memory

    def test_a_newer_schema_is_not_read_and_the_save_is_refused_with_the_bytes_kept(self):
        self.write(json.dumps({"schema": 2, "12": {"dx": 1, "dy": 2}}))
        before = self.raw()
        with self.assertLogs(state.log, "WARNING") as logs:
            self.assertEqual(calibration.load_calibrations(), {})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("newer", logs.output[0])
        with self.assertRaises(calibration.CalibrationNewer) as ctx:
            calibration.save_calibration(12, 5, 5)
        self.assertIn(f"{self.path} is schema 2, newer than 1: not read, not overwritten; "
                      "deploy the newer site", str(ctx.exception))
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_the_route_answers_500_saying_newer_for_a_newer_schema(self):
        self.write(json.dumps({"schema": 2, "12": {"dx": 1, "dy": 2}}))
        before = self.raw()
        body = b'{"area_id": 12, "dx": 5, "dy": 5}'
        req = Request("POST", "/api/calibrate", {}, {"Content-Length": str(len(body))},
                      lambda n: body[:n], "10.0.0.9")
        with self.assertLogs(state.log, "WARNING"):
            status, payload = calibration.calibrate(req)
        self.assertEqual(status, 500)
        self.assertIn(f"{self.path} is schema 2, newer than 1: not read, not overwritten; "
                      "deploy the newer site", payload["error"])
        self.assertEqual(self.raw(), before)

    def test_schema_1_loads_its_zones_without_a_schema_zone(self):
        self.write(json.dumps({"schema": 1, "12": {"dx": 1, "dy": 2}}))
        self.start_up()
        self.assertEqual(calibration.calibrations, {"12": {"dx": 1.0, "dy": 2.0}})
        calibration.save_calibration(13, 3, 4)
        self.assertEqual(json.loads(self.raw()), {"12": {"dx": 1.0, "dy": 2.0},
                                                  "13": {"dx": 3.0, "dy": 4.0}})

    def test_a_schema_that_is_not_a_positive_integer_is_unreadable_not_newer(self):
        for schema in ("1", True, 1.0, 0, -1, None, []):
            with self.subTest(schema=schema):
                self.write(json.dumps({"schema": schema, "12": {"dx": 1, "dy": 2}}))
                before = self.raw()
                with self.assertRaises(calibration.CalibrationUnreadable) as ctx:
                    calibration.save_calibration(13, 1.0, 2.0)
                self.assertNotIsInstance(ctx.exception, calibration.CalibrationNewer)
                self.assertEqual(self.raw(), before)

    def test_the_committed_store_loads_without_a_warning(self):
        # read a copy: a faulty reader must never rewrite the tracked file
        shutil.copy(os.path.join(WOWMAP, "calibration.json"), self.path)
        with self.assertNoLogs(state.log, "WARNING"):
            self.assertEqual(calibration.load_calibrations(), {})


if __name__ == "__main__":
    unittest.main()
