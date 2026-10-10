"""The HTTP layer of agent-runner (#279): routing, token check, the audit line per action.
Named `handler.py`, not `http.py`, so it does not shadow the stdlib `http` package.
`make_server` (runner.py) sets `fleet` and `config` on the class it builds."""

import hmac
import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit

from core import RunnerError, iso

LOG = logging.getLogger("agent-runner")

MAX_BODY_BYTES = 4096
OPERATOR_RE = re.compile(r"[^0-9A-Za-z.:_\-]")


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-runner/1"
    fleet = None
    config = None

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
        except ValueError as err:
            raise RunnerError("body is not valid JSON", status=400) from err
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
