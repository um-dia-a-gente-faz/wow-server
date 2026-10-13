"""The fleet (#279): configured agents, their signals, and the start/stop/create/walk
actions. Docker goes through a backend (docker_backend.py), so tests can hand in a fake."""

import contextlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import characters
from core import RunnerError, iso, scrub
from docker_backend import DockerBackend, allowed_names, image_ref

_CHECKOUT = str(Path(__file__).resolve().parents[2])   # the repo root, for the agents/ library
if _CHECKOUT not in sys.path:
    sys.path.append(_CHECKOUT)
from agents import compose  # noqa: E402
from agents import roster as roster_lib  # noqa: E402

PROBE_TIMEOUT_S = 2.0
# #264: agent API versions this runner understands (agent/api_schema.json; a test pins it).
SUPPORTED_API_VERSIONS = (1,)
CHARACTER_TTL_S = 10.0
AUDIT_TAIL_BYTES = 256 * 1024
UP_TIMEOUT_S = 120
BUILD_TIMEOUT_S = 900
STOP_TIMEOUT_S = 60
MAX_BODY_BYTES = 4096
WALK_DEFAULT_TIMEOUT_S = 60.0
WALK_MAX_TIMEOUT_S = 120.0
WALK_AGENT_SLACK_S = 60.0     # the agent may wait for its think cycle (45 s) before it walks
WALK_KEYS = {"x", "y", "z", "near_player", "map", "stop_distance", "timeout_s"}
AUDIT_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


def _write_atomic(path, text):
    """Write `text` to `path` through a unique temporary file in the same directory: flush, fsync,
    os.replace. On any failure the temporary file is removed, the target is untouched and the
    original exception propagates. The temporary is created exclusively under a random name: for a
    new target with 0o666, so it gets the mode open(..., "w") gave it (minus the umask, applied by
    the kernel per call); for an existing target with 0o600, never wider than that target, then
    fchmod to the target's mode. No directory fsync (ADR 0018).
    Private copy: agent/persist.py, tools/ and scripts/ each carry one until tools/common does."""
    path = os.fspath(path)
    directory, name = os.path.split(path)
    tmp = os.path.join(directory or ".", f".{name}.{secrets.token_hex(8)}.tmp")
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else 0o600)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(text)
            f.flush()
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def parse_docker_time(value):
    """Docker's RFC3339 nano timestamp -> ISO string, or None for the zero time."""
    if not value or value.startswith("0001-"):
        return None
    return value[:19] + "Z"


class Fleet:
    """The configured agents and the signals about each one."""

    def __init__(self, config, character_service=None, backend=None):
        self.cfg = config
        self.backend = backend or DockerBackend(config.project, config.repo, config.compose_path)
        self._character_service = character_service
        self._write_lock = threading.Lock()
        self._create_lock = threading.Lock()  # one create at a time: it spans realm + state + docker
        self._char_cache = {}
        # Two pools: an entry waits on its probes, so they cannot share one.
        self._entry_pool = ThreadPoolExecutor(max_workers=32)
        self._pool = ThreadPoolExecutor(max_workers=16)

    @property
    def characters(self):
        if self._character_service is None:
            self._character_service = characters.CharacterService(
                self.cfg.realm_host, self.cfg.realm_port)
        return self._character_service

    # ── configuration ────────────────────────────────────────────────
    def roster(self):
        """Committed roster plus the runtime `state.json` (D5): its `agents`
        entries replace a roster entry with the same account or are appended."""
        agents = [a.to_json() for a in roster_lib.load_roster(self.cfg.repo / "agents" / "roster.json")]
        state_path = self.cfg.runtime_dir / "state.json"
        if state_path.exists():
            extra = json.loads(state_path.read_text()).get("agents", [])
            by_account = {a["account"]: i for i, a in enumerate(agents)}
            for entry in extra:
                if entry["account"] in by_account:
                    agents[by_account[entry["account"]]] = entry
                else:
                    agents.append(entry)
        entries = []
        for i, a in enumerate(agents):
            agent = compose.derive(i, a)
            entries.append({"name": agent.character, "account": agent.account, "slug": agent.slug,
                            "service": agent.service, "container": agent.container,
                            "port": agent.api_port, "planned": a,
                            "retired": bool(a.get("retired"))})
        return entries

    def find(self, name):
        for e in self.roster():
            if e["name"].lower() == name.lower():
                return e
        raise RunnerError(f"no such agent: {name}", status=404)

    def write_compose(self):
        """Render the runner's own compose file under the runtime dir."""
        text = compose.render([e["planned"] for e in self.roster()], audit_dir=str(self.cfg.audit_dir),
                              fleet_dir=str(self.cfg.runtime_dir / "fleet"))
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.cfg.compose_path, text)

    # ── docker (through the backend, #279) ───────────────────────────
    def containers(self, entries):
        return self.backend.inspect([e["container"] for e in entries], allowed_names(self.roster()))

    def _compose(self, args, service, timeout):
        self.backend.compose(args, service, timeout, allowed_names(self.roster()))

    @staticmethod
    def container_signal(record):
        if record is None:
            return {"state": "absent", "status": None, "exit_code": None, "since": None}
        if "error" in record:
            return {"state": "unknown", "status": None, "exit_code": None, "since": None,
                    "error": record["error"]}
        st = record.get("State", {})
        status = st.get("Status")
        if status in ("running", "restarting", "paused"):
            return {"state": "running", "status": status, "exit_code": None,
                    "since": parse_docker_time(st.get("StartedAt"))}
        return {"state": "exited", "status": status, "exit_code": st.get("ExitCode"),
                "since": parse_docker_time(st.get("FinishedAt"))}

    def image(self):
        """Image build time, and whether `agent/` or the Dockerfile changed since (#131). The image is
        the one WOW_AGENT_TAG names when asked (#730); with no valid tag, docker is not asked. When it
        is not present, `reason` says why: tag_unset, or missing (#732)."""
        ref = image_ref()
        name = ref or "wow-agent:(WOW_AGENT_TAG not set or invalid)"
        created_raw = self.backend.image_created(ref) if ref else None
        if created_raw is None:
            return {"name": name, "created": None, "stale": None, "present": False,
                    "reason": "missing" if ref else "tag_unset"}
        created = parse_docker_time(created_raw)
        stale = None
        try:
            when = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            last = self.backend.last_source_commit()
            if last:
                stale = int(last) > when.timestamp()
        except (ValueError, TypeError):
            stale = None
        return {"name": name, "created": created, "stale": stale, "present": True}

    # ── per-agent signals ────────────────────────────────────────────
    def agent_api(self, port):
        base = f"http://{self.cfg.agent_host}:{port}"
        try:
            health = self._get_json(base + "/healthz")
        except (OSError, ValueError) as e:
            return {"state": "unreachable", "error": scrub(str(e))[:200]}
        v = health.get("api_version")  # #264: absent = older agent, same shape as version 1
        if v is not None and v not in SUPPORTED_API_VERSIONS:
            return {"state": "unsupported", "error": f"agent API version {v} not supported"}
        out = {"state": "ok" if health.get("ok") else "unreachable",
               "connected": health.get("connected")}
        try:
            me = self._get_json(base + "/state").get("self") or {}
            out["position"] = me.get("position")
            out["level"] = me.get("level")
        except (OSError, ValueError):
            pass
        return out

    @staticmethod
    def _get_json(url, timeout=PROBE_TIMEOUT_S):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def audit(self, name, now):
        """Newest record of the agent's audit log: timestamp, age, brain signal."""
        empty = {"last_ts": None, "age_seconds": None}
        agent_dir = self.cfg.audit_dir / name
        try:
            days = sorted((f for f in os.listdir(agent_dir) if AUDIT_FILE.match(f)), reverse=True)
        except OSError:
            return empty, None
        for day in days:
            record = self._last_record(agent_dir / day)
            if record is not None:
                ts = float(record.get("ts") or 0)
                result = record.get("result") or {}
                error = None if result.get("ok") else (result.get("error") or record.get("fallback"))
                brain = {"model": record.get("model"), "valid": record.get("valid"),
                         "last_error": scrub(str(error))[:300] if error else None,
                         "brain": record.get("brain"), "cycle": record.get("cycle")}
                return {"last_ts": iso(ts), "age_seconds": max(0, round(now - ts))}, brain
        return empty, None

    @staticmethod
    def _last_record(path):
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - AUDIT_TAIL_BYTES))
                lines = f.read().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                return record
        return None

    def character(self, name):
        cached = self._char_cache.get(name)
        if cached and time.monotonic() - cached[0] < CHARACTER_TTL_S:
            return cached[1]
        source = f"wowmap {self.cfg.character_url}"
        try:
            c = self._get_json(f"{self.cfg.character_url}/api/character/{quote(name)}")
            out = {"level": c.get("level"), "playtime_seconds": c.get("totaltime"),
                   "zone": c.get("zone_name"), "source": source}
        except urllib.error.HTTPError as e:
            out = {"level": None, "playtime_seconds": None, "zone": None, "source": source,
                   "error": "not found" if e.code == 404 else f"HTTP {e.code}"}
        except (OSError, ValueError) as e:
            out = {"level": None, "playtime_seconds": None, "zone": None, "source": source,
                   "error": scrub(str(e))[:200]}
        self._char_cache[name] = (time.monotonic(), out)
        return out

    # ── views ────────────────────────────────────────────────────────
    def _entry(self, e, record):
        now = time.time()
        container = self.container_signal(record)
        api_f = self._pool.submit(self.agent_api, e["port"]) if container["state"] == "running" else None
        char_f = self._pool.submit(self.character, e["name"])
        audit, brain = self.audit(e["name"], now)
        if api_f is not None:
            agent_api = api_f.result()
        else:
            agent_api = {"state": "unreachable", "error": "container not running"}
        return {"name": e["name"], "account": e["account"], "service": e["service"],
                "retired": e["retired"], "container": container, "agent_api": agent_api, "audit": audit,
                "brain": brain, "character": char_f.result()}

    def status(self):
        entries = self.roster()
        records = self.containers(entries)
        agents = list(self._entry_pool.map(lambda e: self._entry(e, records[e["container"]]), entries))
        return {"agents": agents, "image": self.image(),
                "generated_at": iso(time.time())}

    def one(self, name):
        e = self.find(name)
        return self._entry(e, self.containers([e])[e["container"]])

    # ── actions ──────────────────────────────────────────────────────
    def save_agent(self, planned):
        """Record an agent entry in the runtime state (D5): replaces the entry
        with the same account, or appends. Atomic; never touches the checkout."""
        with self._write_lock:
            state_path = self.cfg.runtime_dir / "state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            agents = [a for a in state.get("agents", []) if a["account"] != planned["account"]]
            agents.append(planned)
            state["agents"] = agents
            self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(state_path, json.dumps(state, indent=2) + "\n")

    def start(self, name, build=False):
        e = self.find(name)
        if e["retired"]:
            raise RunnerError(f"{e['name']} is retired; start is refused", status=409)
        with self._write_lock:
            self.write_compose()
            build = self._build_wanted(build)
            args = ["up", "-d"] + (["--build"] if build else [])
            self._compose(args, e["service"], BUILD_TIMEOUT_S if build else UP_TIMEOUT_S)
        return self.one(name), {"built": build}

    def _build_wanted(self, asked):
        """Whether `start` runs `compose up --build`. A build is wanted when asked (?build=1) or when a
        valid tag names a missing image (#730). A stale image never triggers one (a rollback would
        rebuild the checkout under the old tag), and with no valid tag and no ask compose reports the
        missing variable. #732 Decision: a wanted build runs only when the tag is the checkout's HEAD,
        the commit scripts/build-agent-image.sh tags with; otherwise 409 and compose is not called.
        A start that wants no build reads no git."""
        ref = image_ref()
        if ref is None:
            if asked:
                raise RunnerError("image_not_built", status=409, detail="WOW_AGENT_TAG is unset or not a valid "
                                  "tag, so there is no image to build: set it to the commit's SHA, check out "
                                  "that commit and run scripts/build-agent-image.sh")
            return False
        present = self.backend.image_created(ref) is not None
        if present and not asked:
            return False
        tag, head = ref.partition(":")[2], self.backend.head_commit()
        if head == tag:
            return True
        advice = "check out that commit and run scripts/build-agent-image.sh, or set WOW_AGENT_TAG to an image that exists"
        if present:
            raise RunnerError("tag_is_not_head", status=409,
                              detail=f"{ref} is not the checkout's commit {head or 'unknown'}: {advice}")
        raise RunnerError("image_not_built", status=409,
                          detail=f"{ref} is not built and the checkout is at {head or 'unknown'}: {advice}")

    def stop(self, name):
        e = self.find(name)
        with self._write_lock:
            self.write_compose()
            self._compose(["stop"], e["service"], STOP_TIMEOUT_S)
        return self.one(name), {}

    def retire(self, name):
        """Stop the container and mark the agent retired. The character stays on
        the realm (D6). Idempotent; an already-gone container is fine."""
        e = self.find(name)
        self.stop(name)
        self.save_agent({**e["planned"], "retired": True})
        return self.one(name), {}

    # ── walk (#178) ──────────────────────────────────────────────────
    @staticmethod
    def walk_request(body):
        """Shape-check a walk body and keep only known fields (the agent re-checks all of it)."""
        if not isinstance(body, dict):
            raise RunnerError("body must be a JSON object", status=400)
        unknown = sorted(set(body) - WALK_KEYS)
        if unknown:
            raise RunnerError(f"unknown field(s): {', '.join(unknown)}", status=400)
        has_point, has_player = "x" in body or "y" in body, body.get("near_player") is not None
        if has_point == has_player:
            raise RunnerError("give either x and y (a point) or near_player (a player), not both and not neither",
                              status=400)
        if has_point:
            for key in ("x", "y"):
                v = body.get(key)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 40000:
                    raise RunnerError(f"{key} must be a finite number", status=400)
        elif not isinstance(body["near_player"], str) or not 2 <= len(body["near_player"].strip()) <= 12 \
                or not body["near_player"].strip().isalpha():
            raise RunnerError("near_player must be a character name (2-12 letters)", status=400)
        timeout = body.get("timeout_s")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not 1 <= timeout <= WALK_MAX_TIMEOUT_S):
            raise RunnerError(f"timeout_s must be a number between 1 and {WALK_MAX_TIMEOUT_S:g}", status=400)
        return {k: v for k, v in body.items() if v is not None}

    def walk(self, name, body, dry_run=False):
        """Ask the agent's session to walk -> (http status, body). The body always has
        `ok` and `outcome`; only `outcome == "arrived"` (or "dry_run") is a success.
        Raises RunnerError for refusals and for an agent that could not be asked."""
        req = self.walk_request(body)
        e = self.find(name)
        if e["retired"]:
            raise RunnerError(f"{e['name']} is retired", status=409,
                              extra={"ok": False, "outcome": "refused", "code": "retired"})
        state = self.container_signal(self.containers([e])[e["container"]])
        if state["state"] == "unknown":
            raise RunnerError("could not tell whether the container is running", status=502,
                              detail=state.get("error"),
                              extra={"ok": False, "outcome": "refused", "code": "unknown"})
        if state["state"] != "running":
            raise RunnerError(f"{e['name']}'s container is {state['state']}, not running: start the agent first",
                              status=409, extra={"ok": False, "outcome": "refused", "code": "not_running"})
        timeout = float(req.get("timeout_s") or WALK_DEFAULT_TIMEOUT_S)
        url = f"http://{self.cfg.agent_host}:{e['port']}/control/walk" + ("?dry_run=1" if dry_run else "")
        http_req = urllib.request.Request(url, data=json.dumps(req).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.token}"})
        wait = (0 if dry_run else timeout) + WALK_AGENT_SLACK_S
        try:
            with urllib.request.urlopen(http_req, timeout=wait) as r:
                status, raw = r.status, r.read(MAX_BODY_BYTES * 16)
        except urllib.error.HTTPError as err:
            with err:
                status, raw = err.code, err.read(MAX_BODY_BYTES * 16)
        except (OSError, ValueError) as err:
            refused = isinstance(getattr(err, "reason", err), ConnectionRefusedError)
            raise RunnerError("the agent's API did not answer"
                              + ("" if refused or dry_run else "; the walk may still be running"),
                              status=502, detail=scrub(str(err))[:200],
                              extra={"ok": False, "outcome": "unknown", "code": "agent_unreachable"}) from err
        if status in (401, 405):
            raise RunnerError("the agent does not accept walk commands: its image predates #178 or its control "
                              "token does not match the runner's; start it with a rebuild",
                              status=502, extra={"ok": False, "outcome": "refused", "code": "control_disabled"})
        try:
            result = json.loads(scrub(raw.decode("utf-8", "replace")))
            if not isinstance(result, dict):
                raise ValueError("not an object")
        except ValueError as err:
            raise RunnerError(f"the agent answered HTTP {status} with something other than JSON", status=502,
                              extra={"ok": False, "outcome": "unknown", "code": "bad_answer"}) from err
        result.setdefault("ok", status == 200)
        result.setdefault("outcome", "arrived" if status == 200 else "failed")
        return status, result

    def list_characters(self, account):
        try:
            return self.characters.list(account)
        except characters.CharacterError as err:
            raise RunnerError(scrub(str(err)), status=err.status) from err

    def create_agent(self, body):
        """Create the character on the realm, record it, start its container.
        #734: the order is validation, the roster duplicate, a valid tag (409 tag_unset), then the build
        rule `start` would apply (create asks for no build), all before the first side effect (the account, the character,
        the state, compose). A refusal there answers the start's 409 and changes nothing. A failure
        after the character exists reports exactly what exists."""
        try:
            account, name, race, class_, gender = self.characters.validate(body)
        except characters.CharacterError as err:
            raise RunnerError(scrub(str(err)), status=err.status) from err
        with self._create_lock:
            taken = {e["name"].lower() for e in self.roster() if e["account"] != account}
            if name is not None and name.lower() in taken:
                raise RunnerError(f"name {name!r} is already used by an agent in the roster", status=409)
            if image_ref() is None:   # #734 (second Decision): the start at the end could only fail
                raise RunnerError("tag_unset", status=409,
                                  detail="WOW_AGENT_TAG is unset or not a valid tag in the agent-runner "
                                  "environment, so the new agent could not be started: set it (EnvironmentFile) "
                                  "and restart agent-runner")
            self._build_wanted(False)
            try:
                made = self.characters.create(account, name, race, class_, gender, taken)
            except characters.CharacterError as err:
                raise RunnerError(scrub(str(err)), status=err.status,
                                  extra={"code": err.code} if err.code else None) from err
            planned = {k: made[k] for k in ("account", "character", "race", "class", "gender")}
            partial = {"character_created": True, "account": made["account"],
                       "character": made["character"], "level": made["level"]}
            try:
                self.save_agent(planned)
            except OSError as err:
                raise RunnerError("character created, but runtime state could not be written",
                                  detail=scrub(str(err)),
                                  extra={"partial": {**partial, "recorded": False,
                                                     "container_started": False}}) from err
            try:
                entry, _ = self.start(made["character"])
            except RunnerError as err:
                raise RunnerError("character created and recorded, but its container did not start;"
                                  f" retry with POST /agents/{made['character']}/start",
                                  status=502, detail=err.detail or str(err),
                                  extra={"partial": {**partial, "recorded": True,
                                                     "container_started": False}}) from err
        return entry, {"character": made}
