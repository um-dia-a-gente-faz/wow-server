"""The Docker backend's allow-list (#279). Every name docker is asked about must be a
roster service or container; anything else is refused before `subprocess` runs. No
test here runs docker: `subprocess.run` raises, so a refused name that got through
would fail the test."""

import os
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


class ImageRefTest(unittest.TestCase):
    """#730: the image status follows WOW_AGENT_TAG, read when asked, never `latest`."""

    def setUp(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("WOW_AGENT_TAG", None)

    def test_tag_set_names_the_sha_tagged_image(self):
        os.environ["WOW_AGENT_TAG"] = "3f9c2ab"
        self.assertEqual(docker_backend.image_ref(), "wow-agent:3f9c2ab")

    def test_tag_is_read_at_each_call_not_at_import(self):
        os.environ["WOW_AGENT_TAG"] = "aaa111"
        self.assertEqual(docker_backend.image_ref(), "wow-agent:aaa111")
        os.environ["WOW_AGENT_TAG"] = "bbb222"
        self.assertEqual(docker_backend.image_ref(), "wow-agent:bbb222")

    def test_unset_or_empty_tag_is_none(self):
        self.assertIsNone(docker_backend.image_ref())
        os.environ["WOW_AGENT_TAG"] = ""
        self.assertIsNone(docker_backend.image_ref())

    def test_tag_outside_docker_tag_grammar_is_none(self):
        for tag in ["a b", "x;rm -rf /", "$(id)", "a/b", "v1\nx", "x" * 129]:
            with self.subTest(tag=tag):
                os.environ["WOW_AGENT_TAG"] = tag
                self.assertIsNone(docker_backend.image_ref())

    def test_tag_at_the_edges_of_docker_tag_grammar_is_kept(self):
        os.environ["WOW_AGENT_TAG"] = "x" * 128
        self.assertEqual(docker_backend.image_ref(), "wow-agent:" + "x" * 128)
        os.environ["WOW_AGENT_TAG"] = "v1.2_3-rc"
        self.assertEqual(docker_backend.image_ref(), "wow-agent:v1.2_3-rc")

    def test_image_created_inspects_the_given_ref_as_an_argument_list(self):
        done = subprocess.CompletedProcess([], 0, stdout="2026-10-01T10:00:00.1Z\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            backend = docker_backend.DockerBackend("wow-agents", Path("/checkout"), Path("/runtime/c.yml"))
            self.assertEqual(backend.image_created("wow-agent:3f9c2ab"), "2026-10-01T10:00:00.1Z")
        self.assertEqual(run.call_args.args[0],
                         ["docker", "image", "inspect", "--format", "{{.Created}}", "wow-agent:3f9c2ab"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
