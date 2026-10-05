"""#137: the agent fleet panel — wowmap's proxy to the agent runner (#136).

The runner (tools/agent-runner) owns the Docker socket and the token; wowmap
only forwards. The browser talks to wowmap, wowmap talks to the runner, and the
bearer token (AGENT_RUNNER_TOKEN) is added here, server-side, and scrubbed from
everything that is handed back. Whatever the runner says about a failure is
passed on verbatim, never replaced with a generic "done".

Runner contract (#136):
    GET  /agents                  {"agents": [...], "image": {...}, "generated_at": ...}
    GET  /agents/<name>           one entry
    POST /agents/<name>/start     {"action", "built"?, "agent": entry} or {"error", "detail"}
    POST /agents/<name>/stop      same
    POST /agents/<name>/walk      {"x","y"[,"map"]} or {"near_player"} -> {"ok","outcome","start","end",...}
                                  (#178; 200 only when arrived, 4xx/5xx with the reason otherwise)
An agent entry keeps its signals apart: container.state (running|exited|absent|unknown),
agent_api.state (ok|unreachable) + connected, audit.{last_ts,age_seconds},
brain.{model,valid,last_error}, character.{level,playtime_seconds,zone,source}.

The page half is static/fleet.css, static/fleet.js and the #fleet markup in static/index.html.
"""
import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import quote

log = logging.getLogger("wowmap.fleet")

STATUS_TIMEOUT_S = 10          # the runner asks docker and every agent's API
ACTION_TIMEOUT_S = 930         # the runner's own build timeout is 900 s
WALK_TIMEOUT_S = 200           # the runner waits up to 120 s + 60 s of slack for the agent
MAX_RESPONSE_BYTES = 1024 * 1024
NAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,32}$")
ACTIONS = ("start", "stop")
REDACTED = "[redacted]"


class Runner:
    """Client for one agent runner. `url` empty means none is configured."""

    def __init__(self, url, token):
        self.url = (url or "").strip().rstrip("/")
        self.token = token or ""
        self._roster = []    # {name, account} from the last good answer, so a dead runner still lists rows

    @property
    def configured(self):
        return self.url.startswith(("http://", "https://"))

    def _scrub(self, text):
        return text.replace(self.token, REDACTED) if self.token else text

    def _request(self, method, path, timeout, body=None, headers=None):
        """-> (http status, parsed JSON dict). Raises OSError (unreachable) or
        ValueError (not a JSON object). HTTP errors come back as a status."""
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.url + path, method=method, data=data)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, raw = r.status, r.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as e:
            with e:
                status, raw = e.code, e.read(MAX_RESPONSE_BYTES)
        body = json.loads(self._scrub(raw.decode("utf-8", "replace")))
        if not isinstance(body, dict):
            raise ValueError("runner answered with something other than a JSON object")
        return status, body

    def status(self):
        """The fleet as the page needs it. Never raises: a runner that cannot be
        asked is a state. `agents` then only names the agents the runner last listed
        (no signals), so the page can show each row as unknown instead of nothing."""
        out = {"configured": self.configured, "controls": self.configured,
               "runner": "disabled", "error": None, "agents": [], "image": None,
               "generated_at": None}
        if not self.configured:
            out["error"] = "AGENT_RUNNER_URL is not set"
            return out
        try:
            code, body = self._request("GET", "/agents", STATUS_TIMEOUT_S)
        except (OSError, ValueError) as e:
            log.info("agent runner unreachable: %s", e)
            out.update(runner="unreachable", error="the agent runner did not answer",
                       agents=list(self._roster))
            return out
        if code in (401, 403):
            out.update(runner="unauthorized", agents=list(self._roster),
                       error="the agent runner rejected wowmap's token (check AGENT_RUNNER_TOKEN)")
        elif code != 200 or not isinstance(body.get("agents"), list):
            out.update(runner="error", agents=list(self._roster),
                       error=self._scrub(str(body.get("error") or f"runner returned HTTP {code}"))[:300])
        else:
            out.update(runner="ok", agents=body["agents"], image=body.get("image"),
                       generated_at=body.get("generated_at"))
            self._roster = [{"name": a["name"], "account": a.get("account")}
                            for a in body["agents"] if isinstance(a, dict) and a.get("name")]
        return out

    def act(self, name, action):
        """POST start|stop -> (http status for the browser, body). The runner's
        own error text and detail pass through; its 401 does not: that is wowmap's
        misconfiguration, not something the browser's user can fix."""
        if action not in ACTIONS or not NAME_RE.match(name or ""):
            return 404, {"error": "not found"}
        if not self.configured:
            return 503, {"error": "no agent runner is configured (AGENT_RUNNER_URL is empty); the panel is read-only"}
        try:
            code, body = self._request("POST", f"/agents/{quote(name, safe='')}/{action}", ACTION_TIMEOUT_S)
        except (OSError, ValueError) as e:
            log.warning("agent runner %s %s failed: %s", action, name, e)
            return 502, {"error": "the agent runner did not answer; the action may or may not have run"}
        if code in (401, 403):
            return 502, {"error": "the agent runner rejected wowmap's token (check AGENT_RUNNER_TOKEN)"}
        log.info("fleet %s %s -> runner HTTP %d", action, name, code)
        return code, body


    def walk(self, name, body, dry_run=False, operator=None):
        """POST walk -> (http status for the browser, body). `body` is already the runner's
        shape (walk.build_runner_body). Like `act`: the runner's reason passes through, its
        401 does not, and a runner that cannot be reached says the walk may have run.
        `operator` is the browser's address, passed on so the runner's audit names the person."""
        if not NAME_RE.match(name or ""):
            return 404, {"error": "not found"}
        if not self.configured:
            return 503, {"ok": False, "outcome": "refused", "code": "no_runner",
                         "error": "no agent runner is configured (AGENT_RUNNER_URL is empty); the panel is read-only"}
        headers = {"Content-Type": "application/json"}
        if operator:
            headers["X-Operator-Address"] = operator
        path = f"/agents/{quote(name, safe='')}/walk" + ("?dry_run=1" if dry_run else "")
        try:
            code, out = self._request("POST", path, WALK_TIMEOUT_S, body=body, headers=headers)
        except (OSError, ValueError) as e:
            log.warning("agent runner walk %s failed: %s", name, e)
            return 502, {"ok": False, "outcome": "unknown", "code": "runner_unreachable",
                         "error": "the agent runner did not answer; the walk may or may not have run"}
        if code in (401, 403):
            return 502, {"ok": False, "outcome": "refused", "code": "runner_unauthorized",
                         "error": "the agent runner rejected wowmap's token (check AGENT_RUNNER_TOKEN)"}
        log.info("fleet walk %s%s -> runner HTTP %d (%s)", name, " (dry run)" if dry_run else "", code,
                 out.get("outcome"))
        if code == 200 and out.get("outcome") not in ("arrived", "dry_run"):
            # never let an odd 200 read as success
            out = {**out, "ok": False}
        return code, out
