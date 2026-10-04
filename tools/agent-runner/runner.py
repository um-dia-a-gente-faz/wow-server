#!/usr/bin/env python3
"""agent-runner: fleet status and start/stop for the agent containers (#136, ADR 0002).

One small stdlib-only service on the agent host (`wow-agents`). The console
asks it "what is the fleet doing" and "start/stop this agent", so `wowmap`
never needs the Docker socket.

    GET  /healthz                 {"ok": true}                       (no token)
    GET  /agents                  {"agents": [...], "image": {...}}  (token)
    GET  /agents/<name>           one entry                          (token)
    POST /agents/<name>/start     compose up -d [--build] <service>  (token)
    POST /agents/<name>/stop      compose stop <service>             (token)
    POST /agents                  create a level-1 character (#138)  (token)
    POST /agents/<name>/retire    stop + mark retired, never delete  (token)
    POST /agents/<name>/walk      walk to {x,y[,z]} or {near_player} (#178)  (token)
                                  [?dry_run=1 resolves and plans without moving]
    GET  /characters?account=X    what the realm has for an account  (token)

Each agent entry keeps its signals apart, never one boolean:
  container   running | exited | absent | unknown, + status, exit_code, since
  agent_api   ok | unreachable, from the agent's own /healthz (+ position)
  audit       newest record in <audit_dir>/<agent>/<day>.jsonl and its age
  brain       the last cycle's model, valid, last_error
  character   level, playtime_seconds, zone, and which source said so

`unknown` / null means "could not find out", never "fine".

Environment:
    AGENT_RUNNER_TOKEN        shared token, required (Authorization: Bearer ...)
    AGENT_RUNNER_TOKEN_LABEL  name logged for the token (default "shared")
    AGENT_RUNNER_BIND         bind address (default 127.0.0.1; set the LAN IP)
    AGENT_RUNNER_PORT         listen port (default 9700)
    AGENT_RUNNER_REPO         checkout holding agents/roster.json, the
                              Dockerfile and .env (default: this file's repo)
    AGENT_RUNNER_RUNTIME_DIR  runtime state, outside the checkout
                              (default /opt/wow-agent-runtime)
    AGENT_RUNNER_AUDIT_DIR    agents' audit dir (default <runtime>/audit)
    AGENT_RUNNER_PROJECT      compose project name (default wow-agents)
    AGENT_RUNNER_CHARACTER_URL  wowmap, for level/zone/playtime
                              (default http://192.168.1.64:9400)
    AGENT_RUNNER_AGENT_HOST   where the agents' published APIs answer
                              (default 127.0.0.1)
    AGENT_RUNNER_REALM_HOST   realm for character creation (default 192.168.1.64)
    AGENT_RUNNER_REALM_PORT   its auth port (default 3724)
    AGENT_PASSWORD            shared account password; used to log in and to
                              create accounts, never returned, logged or stored

Walk (#178) forwards the command to the agent's own session, which walks with its own
movement code (agent/control.py): never a teleport, a GM command or a database write.
The runner checks the container is running, passes its token to the agent as
AGENT_CONTROL_TOKEN (compose maps it from AGENT_RUNNER_TOKEN), and logs one line per
command with caller IP, operator, agent, target, outcome and start/end positions.

Never touches the worldserver, a GM command or the realm database. Output from
docker is scrubbed of the token and of every *PASSWORD/*KEY/*TOKEN/*SECRET
value in the environment before it is logged or returned.
"""

import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import characters

LOG = logging.getLogger("agent-runner")

REPO_DEFAULT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 9700
DEFAULT_RUNTIME_DIR = "/opt/wow-agent-runtime"
DEFAULT_PROJECT = "wow-agents"
DEFAULT_CHARACTER_URL = "http://192.168.1.64:9400"
IMAGE = "wow-agent:latest"

PROBE_TIMEOUT_S = 2.0
CHARACTER_TTL_S = 10.0
AUDIT_TAIL_BYTES = 256 * 1024
UP_TIMEOUT_S = 120
BUILD_TIMEOUT_S = 900
STOP_TIMEOUT_S = 60
DOCKER_TIMEOUT_S = 15
MAX_DETAIL_CHARS = 600
MAX_BODY_BYTES = 4096
WALK_DEFAULT_TIMEOUT_S = 60.0
WALK_MAX_TIMEOUT_S = 120.0
WALK_AGENT_SLACK_S = 60.0     # the agent may wait for its think cycle (45 s) before it walks
WALK_KEYS = {"x", "y", "z", "near_player", "map", "stop_distance", "timeout_s"}
OPERATOR_RE = re.compile(r"[^0-9A-Za-z.:_\-]")
SECRET_NAME = re.compile(r"PASSWORD|KEY|TOKEN|SECRET", re.I)
AUDIT_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


class RunnerError(Exception):
    """An action that failed in a way the caller should see (HTTP `status`)."""

    def __init__(self, message, status=502, detail=None, extra=None):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.extra = extra or {}

    def body(self):
        return {"error": str(self), "detail": self.detail, **self.extra}


def scrub(text, extra=()):
    """Remove secret values (environment and `extra`) from text."""
    secrets = {v for k, v in os.environ.items() if SECRET_NAME.search(k) and len(v) >= 4}
    secrets.update(v for v in extra if v and len(v) >= 4)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_docker_time(value):
    """Docker's RFC3339 nano timestamp -> ISO string, or None for the zero time."""
    if not value or value.startswith("0001-"):
        return None
    return value[:19] + "Z"


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.token = env.get("AGENT_RUNNER_TOKEN", "")
        self.token_label = env.get("AGENT_RUNNER_TOKEN_LABEL", "shared")
        self.bind = env.get("AGENT_RUNNER_BIND", "127.0.0.1")
        self.port = int(env.get("AGENT_RUNNER_PORT", DEFAULT_PORT))
        self.repo = Path(env.get("AGENT_RUNNER_REPO", REPO_DEFAULT))
        self.runtime_dir = Path(env.get("AGENT_RUNNER_RUNTIME_DIR", DEFAULT_RUNTIME_DIR))
        self.audit_dir = Path(env.get("AGENT_RUNNER_AUDIT_DIR", str(self.runtime_dir / "audit")))
        self.project = env.get("AGENT_RUNNER_PROJECT", DEFAULT_PROJECT)
        self.character_url = env.get("AGENT_RUNNER_CHARACTER_URL", DEFAULT_CHARACTER_URL).rstrip("/")
        self.agent_host = env.get("AGENT_RUNNER_AGENT_HOST", "127.0.0.1")
        self.realm_host = env.get("AGENT_RUNNER_REALM_HOST", "192.168.1.64")
        self.realm_port = int(env.get("AGENT_RUNNER_REALM_PORT", 3724))

    @property
    def compose_path(self):
        return self.runtime_dir / "docker-compose.agents.yml"


def load_generator(repo):
    """scripts/gen_agents_compose.py from the checkout: the roster's compose renderer."""
    sys.path.insert(0, str(repo / "scripts"))
    try:
        import gen_agents_compose
    finally:
        sys.path.pop(0)
    return gen_agents_compose


class Fleet:
    """The configured agents and the signals about each one."""

    def __init__(self, config, gen=None, character_service=None):
        self.cfg = config
        self.gen = gen or load_generator(config.repo)
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
            car = characters.load_script(self.cfg.repo, "create_agent_roster")
            self._character_service = characters.CharacterService(
                car, self.cfg.realm_host, self.cfg.realm_port)
        return self._character_service

    # ── configuration ────────────────────────────────────────────────
    def roster(self):
        """Committed roster plus the runtime `state.json` (D5): its `agents`
        entries replace a roster entry with the same account or are appended."""
        agents = self.gen.load_roster(self.cfg.repo / "agents" / "roster.json")
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
            slug = a["character"].lower()
            entries.append({"name": a["character"], "account": a["account"], "slug": slug,
                            "service": f"agent-{slug}", "container": f"wow-agent-{slug}",
                            "port": self.gen.FIRST_PORT + i, "planned": a,
                            "retired": bool(a.get("retired"))})
        return entries

    def find(self, name):
        for e in self.roster():
            if e["name"].lower() == name.lower():
                return e
        raise RunnerError(f"no such agent: {name}", status=404)

    def write_compose(self):
        """Render the runner's own compose file under the runtime dir."""
        text = self.gen.render([e["planned"] for e in self.roster()], audit_dir=str(self.cfg.audit_dir))
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.compose_path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.cfg.compose_path)

    # ── docker ───────────────────────────────────────────────────────
    def docker(self, args, timeout=DOCKER_TIMEOUT_S):
        try:
            return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise RunnerError("docker is not installed on this host", detail=str(e))
        except subprocess.TimeoutExpired:
            raise RunnerError(f"docker {args[0]} timed out after {timeout}s")

    def compose(self, args, timeout):
        base = ["compose", "--project-directory", str(self.cfg.repo), "-p", self.cfg.project,
                "-f", str(self.cfg.compose_path)]
        res = self.docker(base + args, timeout=timeout)
        if res.returncode != 0:
            detail = scrub((res.stderr or res.stdout or "").strip())[-MAX_DETAIL_CHARS:]
            raise RunnerError(f"docker compose {args[0]} failed (exit {res.returncode})", detail=detail)

    def containers(self, entries):
        """name -> inspect record (or None if absent), or {"error": ...} for all
        when docker itself cannot answer: unknown must not read as absent."""
        res = self.docker(["inspect", *[e["container"] for e in entries]])
        try:
            found = {r["Name"].lstrip("/"): r for r in json.loads(res.stdout or "[]")}
        except (ValueError, KeyError):
            found = {}
        if res.returncode != 0 and not found and "no such object" not in (res.stderr or "").lower():
            err = scrub((res.stderr or "docker inspect failed").strip())[-MAX_DETAIL_CHARS:]
            return {e["container"]: {"error": err} for e in entries}
        return {e["container"]: found.get(e["container"]) for e in entries}

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
        """Image build time, and whether `agent/` or the Dockerfile changed since (#131)."""
        res = self.docker(["image", "inspect", "--format", "{{.Created}}", IMAGE])
        if res.returncode != 0:
            return {"name": IMAGE, "created": None, "stale": None, "present": False}
        created = parse_docker_time(res.stdout.strip())
        stale = None
        try:
            git = subprocess.run(["git", "-C", str(self.cfg.repo), "log", "-1", "--format=%ct",
                                  "--", "agent", "Dockerfile"], capture_output=True, text=True,
                                 timeout=DOCKER_TIMEOUT_S)
            when = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if git.returncode == 0 and git.stdout.strip():
                stale = int(git.stdout.strip()) > when.timestamp()
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            stale = None
        return {"name": IMAGE, "created": created, "stale": stale, "present": True}

    # ── per-agent signals ────────────────────────────────────────────
    def agent_api(self, port):
        base = f"http://{self.cfg.agent_host}:{port}"
        try:
            health = self._get_json(base + "/healthz")
        except (OSError, ValueError) as e:
            return {"state": "unreachable", "error": scrub(str(e))[:200]}
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
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2) + "\n")
            os.replace(tmp, state_path)

    def start(self, name, build=False):
        e = self.find(name)
        if e["retired"]:
            raise RunnerError(f"{e['name']} is retired; start is refused", status=409)
        with self._write_lock:
            self.write_compose()
            image = self.image()
            build = build or not image["present"] or image["stale"] is True
            args = ["up", "-d"] + (["--build"] if build else []) + [e["service"]]
            self.compose(args, BUILD_TIMEOUT_S if build else UP_TIMEOUT_S)
        return self.one(name), {"built": build}

    def stop(self, name):
        e = self.find(name)
        with self._write_lock:
            self.write_compose()
            self.compose(["stop", e["service"]], STOP_TIMEOUT_S)
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
                              extra={"ok": False, "outcome": "unknown", "code": "agent_unreachable"})
        if status in (401, 405):
            raise RunnerError("the agent does not accept walk commands: its image predates #178 or its control "
                              "token does not match the runner's; start it with a rebuild",
                              status=502, extra={"ok": False, "outcome": "refused", "code": "control_disabled"})
        try:
            result = json.loads(scrub(raw.decode("utf-8", "replace")))
            if not isinstance(result, dict):
                raise ValueError("not an object")
        except ValueError:
            raise RunnerError(f"the agent answered HTTP {status} with something other than JSON", status=502,
                              extra={"ok": False, "outcome": "unknown", "code": "bad_answer"})
        result.setdefault("ok", status == 200)
        result.setdefault("outcome", "arrived" if status == 200 else "failed")
        return status, result

    def list_characters(self, account):
        try:
            return self.characters.list(account)
        except characters.CharacterError as err:
            raise RunnerError(scrub(str(err)), status=err.status)

    def create_agent(self, body):
        """Create the character on the realm, record it, start its container.
        A failure after the character exists reports exactly what exists."""
        try:
            account, name, race, class_, gender = self.characters.validate(body)
        except characters.CharacterError as err:
            raise RunnerError(scrub(str(err)), status=err.status)
        with self._create_lock:
            taken = {e["name"].lower() for e in self.roster() if e["account"] != account}
            try:
                made = self.characters.create(account, name, race, class_, gender, taken)
            except characters.CharacterError as err:
                raise RunnerError(scrub(str(err)), status=err.status,
                                  extra={"code": err.code} if err.code else None)
            planned = {k: made[k] for k in ("account", "character", "race", "class", "gender")}
            partial = {"character_created": True, "account": made["account"],
                       "character": made["character"], "level": made["level"]}
            try:
                self.save_agent(planned)
            except OSError as err:
                raise RunnerError("character created, but runtime state could not be written",
                                  detail=scrub(str(err)),
                                  extra={"partial": {**partial, "recorded": False,
                                                     "container_started": False}})
            try:
                entry, _ = self.start(made["character"])
            except RunnerError as err:
                raise RunnerError("character created and recorded, but its container did not start;"
                                  f" retry with POST /agents/{made['character']}/start",
                                  status=502, detail=err.detail or str(err),
                                  extra={"partial": {**partial, "recorded": True,
                                                     "container_started": False}})
        return entry, {"character": made}


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-runner/1"
    fleet: Fleet = None
    config: Config = None

    def log_message(self, fmt, *args):
        LOG.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        return hmac.compare_digest(supplied.encode("utf-8"), self.config.token.encode("utf-8"))

    def _audit_write(self, action, agent, result, **extra):
        LOG.info(json.dumps({"ts": iso(time.time()), "action": action, "agent": agent,
                             "caller": self.client_address[0],
                             "token": self.config.token_label, "result": result, **extra}))

    def _operator(self):
        """Who asked wowmap, when wowmap says (X-Operator-Address): the audit then shows the
        person's address next to wowmap's own, which is the `caller`."""
        return OPERATOR_RE.sub("", self.headers.get("X-Operator-Address", ""))[:45] or None

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY_BYTES:
            raise RunnerError("a JSON object body is required", status=400)
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise RunnerError("body is not valid JSON", status=400)
        if not isinstance(body, dict):
            raise RunnerError("body must be a JSON object", status=400)
        return body

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, {"ok": True})
        parts = [unquote(p) for p in path.split("/")[1:]]
        if parts[0] not in ("agents", "characters") or len(parts) > (1 if parts[0] == "characters" else 2):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            self._audit_write(f"GET {path}", None, "401")
            return self._send(401, {"error": "token required"})
        try:
            if parts[0] == "characters":
                account = parse_qs(url.query).get("account", [""])[0]
                return self._send(200, self.fleet.list_characters(account))
            if len(parts) == 1:
                return self._send(200, self.fleet.status())
            return self._send(200, self.fleet.one(parts[1]))
        except RunnerError as e:
            return self._send(e.status, e.body())

    @staticmethod
    def _walk_audit(request, status, result):
        """The audit fields of one walk: target, outcome and where the agent started and ended."""
        request = request if isinstance(request, dict) else {}
        target = result.get("target") or {k: request.get(k) for k in ("x", "y", "z", "near_player")
                                          if k in request}
        return {"target": target, "outcome": result.get("outcome"), "code": result.get("code"),
                "dry_run": bool(result.get("dry_run")), "start": result.get("start"),
                "end": result.get("end"), "http": status}

    def _walk(self, name, query):
        dry = "dry_run=1" in query.split("&")
        action = "walk-dry-run" if dry else "walk"
        body = {}
        try:
            body = self._read_json()
            status, result = self.fleet.walk(name, body, dry_run=dry)
        except RunnerError as e:
            result = e.body()
            self._audit_write(action, name, f"error: {e}", operator=self._operator(),
                              **self._walk_audit(body, e.status, result))
            return self._send(e.status, result)
        ok = result.get("outcome") in ("arrived", "dry_run")
        self._audit_write(action, name, "ok" if ok else f"error: {result.get('outcome')}: {result.get('error')}",
                          operator=self._operator(), **self._walk_audit(body, status, result))
        return self._send(status, {"action": action, **result})

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        parts = [unquote(p) for p in path.split("/")[1:]]
        create = parts == ["agents"]
        if not create and (len(parts) != 3 or parts[0] != "agents"
                           or parts[2] not in ("start", "stop", "retire", "walk")):
            return self._send(404, {"error": "not found"})
        name, action = (None, "create") if create else (parts[1], parts[2])
        if not self._authorized():
            self._audit_write(action, name, "401")
            return self._send(401, {"error": "token required"})
        if action == "walk":
            return self._walk(name, urlsplit(self.path).query)
        try:
            if create:
                body = self._read_json()
                name = body.get("account")
                entry, extra = self.fleet.create_agent(body)
                name = entry["name"]
            elif action == "start":
                query = urlsplit(self.path).query
                entry, extra = self.fleet.start(name, build="build=1" in query.split("&"))
            elif action == "retire":
                entry, extra = self.fleet.retire(name)
            else:
                entry, extra = self.fleet.stop(name)
        except RunnerError as e:
            self._audit_write(action, name, f"error: {e}")
            return self._send(e.status, e.body())
        self._audit_write(action, name, "ok")
        return self._send(201 if create else 200, {"action": action, **extra, "agent": entry})


def make_server(config, fleet=None):
    handler = type("BoundHandler", (Handler,), {"fleet": fleet or Fleet(config), "config": config})
    return ThreadingHTTPServer((config.bind, config.port), handler)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config()
    if not config.token:
        sys.exit("AGENT_RUNNER_TOKEN is not set; refusing to start without auth")
    if config.bind in ("0.0.0.0", "::"):
        LOG.warning("bound to every interface; bind the LAN address with AGENT_RUNNER_BIND")
    server = make_server(config)
    LOG.info("agent-runner on %s:%d (repo %s, runtime %s)", config.bind, config.port,
             config.repo, config.runtime_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
