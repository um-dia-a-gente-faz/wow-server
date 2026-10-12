"""#670: no wowmap test process reads or writes the tracked calibration.json.

The helper _calibration_env points CALIBRATION_FILE at a temporary file. These tests check that
every test module reaching a wowmap module imports the helper first, and that the production
default (the file next to calibration.py) is unchanged."""
import ast
import filecmp
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import _calibration_env  # noqa: F401 (#670: before any wowmap import)

TESTS = pathlib.Path(__file__).resolve().parent
WOWMAP = TESTS.parent / "wowmap"
PIPELINE = TESTS.parent / "pipeline"
TOOLS = WOWMAP.parents[1]
TRACKED = os.path.realpath(WOWMAP / "calibration.json")
HELPER = "_calibration_env"


def wowmap_names():
    """Top-level names a test can import from the runtime: wowmap/*.py, its packages, and the
    pipeline scripts, which import wowmap modules themselves."""
    names = {p.stem for p in WOWMAP.glob("*.py")}
    names |= {p.name for p in WOWMAP.iterdir() if p.is_dir() and any(p.glob("*.py"))}
    names |= {p.stem for p in PIPELINE.glob("*.py")}
    return names


def imported_names(path):
    """(line, top-level name) for every import in the file, at any depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name.split(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


class ProductionDefault(unittest.TestCase):
    def test_the_default_store_is_the_file_next_to_the_module(self):
        # A copy of the runtime package without its calibration.json, not the tracked module:
        # importing the tracked module runs its load, and a faulty reader must not write the
        # tracked file from here. The copy is byte-identical to the module.
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "wowmap"
            shutil.copytree(WOWMAP, copy, ignore=shutil.ignore_patterns("calibration.json", "__pycache__"))
            self.assertTrue(filecmp.cmp(copy / "calibration.py", WOWMAP / "calibration.py", shallow=False))
            env = {k: v for k, v in os.environ.items() if k != "CALIBRATION_FILE"}
            env["PYTHONPATH"] = f"{copy}{os.pathsep}{TOOLS}"
            code = ("import calibration, os\n"
                    "print(calibration.CALIBRATION_FILE)\n"
                    "print(os.path.dirname(calibration.__file__))\n")
            r = subprocess.run([sys.executable, "-c", code], cwd=copy, env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            default, directory = r.stdout.split("\n")[:2]
            self.assertEqual(default, os.path.join(directory, "calibration.json"))
            self.assertEqual(os.path.realpath(directory), os.path.realpath(copy))


class HelperFirst(unittest.TestCase):
    def test_this_process_uses_a_temporary_store_not_the_tracked_file(self):
        self.assertNotEqual(os.path.realpath(os.environ["CALIBRATION_FILE"]), TRACKED)
        self.assertTrue(os.environ["CALIBRATION_FILE"].startswith(tempfile.gettempdir()))

    def test_every_test_module_that_reaches_wowmap_imports_the_helper_before_it(self):
        wowmap = wowmap_names()
        self.assertIn("calibration", wowmap)
        found = {p.stem: imported_names(p) for p in TESTS.glob("*.py") if p.stem != HELPER}
        # a test helper that imports wowmap (pagesrc) reaches it too, and so does any module importing it
        reaching = set(wowmap)
        grew = True
        while grew:
            grew = False
            for stem, names in found.items():
                if stem not in reaching and any(name in reaching for _, name in names):
                    reaching.add(stem)
                    grew = True
        self.assertIn("test_calibration", reaching)
        late = []
        for stem, names in sorted(found.items()):
            first_wowmap = [line for line, name in names if name in reaching]
            if not first_wowmap:
                continue
            helper = [line for line, name in names if name == HELPER]
            if not helper or min(helper) > min(first_wowmap):
                late.append(f"{stem}.py")
        self.assertEqual(late, [], "these modules import a wowmap module before the helper")


if __name__ == "__main__":
    unittest.main()
