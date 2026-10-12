"""The shuffle runner's argument check and repo-root lookup (#684). The suite itself is never run here."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from agent.tests import shuffle_run


class UsageTest(unittest.TestCase):
    def test_bad_arguments_print_the_usage_and_exit_2_without_running_anything(self):
        cases = {"unknown mode": ["x", "1", "modul"], "non-numeric seed": ["x", "abc"],
                 "missing seed": ["x"], "extra argument": ["x", "1", "test", "more"]}
        for name, argv in cases.items():
            with self.subTest(name), mock.patch.object(shuffle_run, "shuffled") as shuffled, \
                    mock.patch.object(shuffle_run.unittest, "TextTestRunner") as runner, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(shuffle_run.main(argv), 2)
            self.assertEqual(err.getvalue(), shuffle_run.USAGE + "\n")
            shuffled.assert_not_called()
            runner.assert_not_called()


class RepoRootTest(unittest.TestCase):
    def test_discovery_uses_the_repo_root_whatever_the_working_directory(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            with mock.patch.object(shuffle_run.unittest.defaultTestLoader, "discover",
                                   return_value=unittest.TestSuite()) as discover:
                shuffle_run.shuffled(1)
            os.chdir(cwd)
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(shuffle_run.__file__))))
        discover.assert_called_once_with(os.path.join(repo, "agent", "tests"), top_level_dir=repo)
        self.assertTrue(os.path.isfile(os.path.join(repo, "scripts", "check.sh")))


if __name__ == "__main__":
    unittest.main()
