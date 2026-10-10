"""The only agent-runner module that runs `docker` or `git` (#279, ADR 0002 D2).

Every name handed to docker must be a roster service or container: `check_names` is
the allow-list, and every method that names a container or service calls it before
`subprocess` runs. `FakeDockerBackend` is the test double for `Fleet`.
"""

import json
import subprocess

from core import RunnerError, scrub

IMAGE = "wow-agent:latest"
DOCKER_TIMEOUT_S = 15
MAX_DETAIL_CHARS = 600


def allowed_names(entries):
    """The roster's service and container names: the only names docker may be asked about."""
    return frozenset(n for e in entries for n in (e["service"], e["container"]))


def check_names(names, allowed):
    """Refuse any name that is not in the roster, before docker runs."""
    refused = [n for n in names if n not in allowed]
    if refused:
        raise RunnerError("refusing docker names that are not roster agents: "
                          + ", ".join(repr(n) for n in refused), status=400)


class DockerBackend:
    def __init__(self, project, repo, compose_path):
        self.project = project
        self.repo = repo
        self.compose_path = compose_path

    def _run(self, args, timeout=DOCKER_TIMEOUT_S):
        try:
            return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise RunnerError("docker is not installed on this host", detail=str(e)) from e
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"docker {args[0]} timed out after {timeout}s") from e

    def inspect(self, names, allowed):
        """name -> inspect record (None if absent), or {"error": ...} for every name when
        docker itself cannot answer: unknown must not read as absent."""
        check_names(names, allowed)
        res = self._run(["inspect", *names])
        try:
            found = {r["Name"].lstrip("/"): r for r in json.loads(res.stdout or "[]")}
        except (ValueError, KeyError):
            found = {}
        if res.returncode != 0 and not found and "no such object" not in (res.stderr or "").lower():
            err = scrub((res.stderr or "docker inspect failed").strip())[-MAX_DETAIL_CHARS:]
            return {n: {"error": err} for n in names}
        return {n: found.get(n) for n in names}

    def compose(self, verb_args, service, timeout, allowed):
        """`docker compose <verb_args> <service>` on the runner's own compose file."""
        check_names([service], allowed)
        args = ["compose", "--project-directory", str(self.repo), "-p", self.project,
                "-f", str(self.compose_path), *verb_args, service]
        res = self._run(args, timeout=timeout)
        if res.returncode != 0:
            detail = scrub((res.stderr or res.stdout or "").strip())[-MAX_DETAIL_CHARS:]
            raise RunnerError(f"docker compose {verb_args[0]} failed (exit {res.returncode})", detail=detail)

    def image_created(self):
        """The image's Created time as docker prints it, or None when the image is absent."""
        res = self._run(["image", "inspect", "--format", "{{.Created}}", IMAGE])
        return res.stdout.strip() if res.returncode == 0 else None

    def last_source_commit(self):
        """Unix time (text) of the last commit touching agent/ or the Dockerfile; "" when
        git cannot say."""
        try:
            git = subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%ct",
                                  "--", "agent", "Dockerfile"], capture_output=True, text=True,
                                 timeout=DOCKER_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return ""
        return git.stdout.strip() if git.returncode == 0 else ""


class FakeDockerBackend:
    """Test double (#279). Answers from what it was given and records each call:
    ("inspect", names), ("compose", verb_args, service), ("image",), ("source",)."""

    def __init__(self, containers=None, image_created=None, source_commit="", compose_error=None):
        self.containers = containers or {}
        self._image_created = image_created
        self._source_commit = source_commit
        self.compose_error = compose_error
        self.calls = []

    def inspect(self, names, allowed):
        check_names(names, allowed)
        self.calls.append(("inspect", list(names)))
        return {n: self.containers.get(n) for n in names}

    def compose(self, verb_args, service, timeout, allowed):
        check_names([service], allowed)
        self.calls.append(("compose", list(verb_args), service))
        if self.compose_error:
            raise RunnerError(f"docker compose {verb_args[0]} failed (exit 1)", detail=self.compose_error)

    def image_created(self):
        self.calls.append(("image",))
        return self._image_created

    def last_source_commit(self):
        self.calls.append(("source",))
        return self._source_commit
