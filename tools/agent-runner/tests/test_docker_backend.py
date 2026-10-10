"""The Docker backend's allow-list (#279). Every name docker is asked about must be a
roster service or container; anything else is refused before `subprocess` runs. No
test here runs docker: `subprocess.run` raises, so a refused name that got through
would fail the test."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import docker_backend  # noqa: E402
from core import RunnerError  # noqa: E402

ROSTER = [{"service": "agent-alpha", "container": "wow-agent-alpha"},
          {"service": "agent-bravo", "container": "wow-agent-bravo"}]
ALLOWED = docker_backend.allowed_names(ROSTER)
ARBITRARY = ["../x", "host", "", "wow-agent-zulu", "agent-zulu", "-f", "wow-agent-alpha --privileged",
             "other-project-wow-agent-alpha", "wow-agent-alpha/../../etc"]


class AllowListTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(subprocess, "run", side_effect=AssertionError("docker must not run"))
        patch.start()
        self.addCleanup(patch.stop)
        self.backend = docker_backend.DockerBackend("wow-agents", Path("/checkout"), Path("/runtime/c.yml"))

    def test_roster_names_pass_the_check(self):
        docker_backend.check_names(["agent-alpha", "wow-agent-bravo"], ALLOWED)

    def test_inspect_refuses_arbitrary_names_before_docker(self):
        for name in ARBITRARY:
            with self.subTest(name=name), self.assertRaises(RunnerError):
                self.backend.inspect([name], ALLOWED)
        with self.assertRaises(RunnerError):
            self.backend.inspect(["wow-agent-alpha", "../x"], ALLOWED)

    def test_compose_refuses_arbitrary_services_before_docker(self):
        for name in ARBITRARY:
            with self.subTest(name=name), self.assertRaises(RunnerError):
                self.backend.compose(["up", "-d"], name, 5, ALLOWED)


if __name__ == "__main__":
    unittest.main()
