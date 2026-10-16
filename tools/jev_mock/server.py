#!/usr/bin/env python3
"""jev-mock: a local, stdlib-only stand-in for OpenRouter's Decisions API
(UM-96, ADR 0001).

Speaks the same HTTP contract as the real endpoint so agent.jev.JevClient
works against it unchanged (point JEV_BASE_URL at http://<host>:<port>/api/alpha):

    POST /api/alpha/decisions
      {"model", "state", "questions": {name: {"type": "choice",
       "instructions", "criteria": {option_key: description}}}}
    200 {"id", "model", "provider", "answers": {name: {"type": "choice",
         "choice", "confidence", "probabilities": {option_key: p}}},
         "usage": {"input_tokens", "output_tokens", "cost"}}
    4xx/5xx {"error": {"code", "message"}}

Shape checked against OpenRouter's Decisions API reference and Jev tutorial
(openrouter.ai/docs, 2026-10-02). The policy is deliberately random: the
choice is a uniformly random option key, and the probabilities are a random
distribution with the choice as its most likely option. Only `choice`
questions are supported; `noul`/`score` get a 400.

Environment:
    HOST                   bind address (default 127.0.0.1)
    PORT                   listen port (default 8090)
    JEV_MOCK_SEED          seed the RNG for reproducible answers
    JEV_MOCK_STATUS        answer every decisions request with this error
                           status (e.g. 402 out of credits, 429 rate limited)
    JEV_MOCK_REQUIRE_AUTH  "1": 401 without an `Authorization: Bearer ...`
                           header, as the real API does (any token is accepted)
"""

import json
import logging
import math
import os
import random
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("jev-mock")

DECISIONS_PATH = "/api/alpha/decisions"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
MAX_BODY_BYTES = 1 << 20

# Messages for JEV_MOCK_STATUS, modelled on OpenRouter's error examples.
ERROR_MESSAGES = {
    400: "Invalid request parameters",
    401: "Missing Authentication header",
    402: "Insufficient credits. Add more using https://openrouter.ai/credits",
    403: "Forbidden",
    408: "Request timed out",
    429: "Rate limit exceeded",
    500: "Internal Server Error",
    502: "Provider returned error",
    503: "Service temporarily unavailable",
}


class BadRequest(Exception):
    pass


def validate(body) -> dict:
    """Return the questions dict, or raise BadRequest with the reason."""
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    for field in ("model", "state", "questions"):
        if field not in body:
            raise BadRequest(f"missing required field: {field}")
    if not isinstance(body["model"], str) or not body["model"]:
        raise BadRequest("model must be a non-empty string")
    if not isinstance(body["state"], (str, dict, list)):
        raise BadRequest("state must be a string, object or array")
    questions = body["questions"]
    if not isinstance(questions, dict) or not questions:
        raise BadRequest("questions must be a non-empty object")
    for name, q in questions.items():
        if not isinstance(q, dict):
            raise BadRequest(f"questions.{name} must be an object")
        if q.get("type") != "choice":
            raise BadRequest(f"questions.{name}.type {q.get('type')!r} is not supported "
                             "by jev-mock (only 'choice')")
        if "instructions" not in q:
            raise BadRequest(f"questions.{name}.instructions is required")
        criteria = q.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise BadRequest(f"questions.{name}.criteria must be a non-empty object")
    return questions


def answer_choice(criteria: dict, rng: random.Random) -> dict:
    """A random but internally consistent choice answer."""
    keys = list(criteria)
    choice = rng.choice(keys)
    weights = sorted((rng.random() for _ in keys), reverse=True)
    others = [k for k in keys if k != choice]
    rng.shuffle(others)
    total = sum(weights)
    probs = {k: w / total for k, w in zip([choice] + others, weights)}
    # Confidence: 1 - normalized entropy, i.e. how concentrated the
    # distribution is (the meaning the Jev tutorial gives it).
    if len(keys) > 1:
        entropy = -sum(p * math.log(p) for p in probs.values() if p > 0)
        confidence = 1.0 - entropy / math.log(len(keys))
    else:
        confidence = 1.0
    return {"type": "choice", "choice": choice, "confidence": round(confidence, 4),
            "probabilities": {k: round(probs[k], 4) for k in keys}}


def decide(body: dict, raw_len: int, rng: random.Random) -> dict:
    questions = validate(body)
    return {
        "id": f"gen-dec-mock-{uuid.uuid4().hex[:20]}",
        "model": body["model"],
        "provider": "jev-mock",
        "answers": {name: answer_choice(q["criteria"], rng) for name, q in questions.items()},
        # Rough token estimate; Jev bills input only, the mock bills nothing.
        "usage": {"input_tokens": max(1, raw_len // 4), "output_tokens": 0, "cost": 0.0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "jev-mock/1"

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        self._error(404, f"no route for GET {self.path}")

    def do_POST(self):
        if self.path.split("?", 1)[0] != DECISIONS_PATH:
            return self._error(404, f"no route for POST {self.path}")
        srv = self.server
        if srv.require_auth and not (self.headers.get("Authorization") or "").startswith("Bearer "):
            return self._error(401)
        if srv.force_status:
            return self._error(srv.force_status)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(400, "bad Content-Length")
        if length > MAX_BODY_BYTES:
            return self._error(413, "payload too large")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error(400, "request body is not valid JSON")
        try:
            with srv.rng_lock:
                response = decide(body, len(raw), srv.rng)
        except BadRequest as e:
            return self._error(400, str(e))
        self._send(200, response)

    def _error(self, status: int, message: str = ""):
        self._send(status, {"error": {"code": status,
                                      "message": message or ERROR_MESSAGES.get(status, "error")}})

    def _send(self, status: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, seed=None,
                force_status: int = 0, require_auth: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.rng = random.Random(seed)
    server.rng_lock = threading.Lock()
    server.force_status = force_status
    server.require_auth = require_auth
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    seed = os.environ.get("JEV_MOCK_SEED")
    server = make_server(
        host=os.environ.get("HOST", DEFAULT_HOST),
        port=int(os.environ.get("PORT", DEFAULT_PORT)),
        seed=int(seed) if seed else None,
        force_status=int(os.environ.get("JEV_MOCK_STATUS") or 0),
        require_auth=os.environ.get("JEV_MOCK_REQUIRE_AUTH") == "1",
    )
    host, port = server.server_address[:2]
    LOG.info("jev-mock listening on http://%s:%d (JEV_BASE_URL=http://%s:%d/api/alpha)",
             host, port, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
