"""A JSON request handler base for the tools' http.server services (#263). Stdlib only.

Subclass JsonHandler, call read_json / check_bearer inside a do_* method, and
catch HttpError to answer with send_error_json. Every error body is
{"error": message}.
"""
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler

LOG = logging.getLogger("jsonserver")


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class JsonHandler(BaseHTTPRequestHandler):
    MAX_BODY = 1 << 20  # bytes; a larger Content-Length is refused before it is read

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def read_json(self):
        """The request body as a dict. Raises HttpError 400/413 otherwise."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "bad content-length") from None
        if length < 0:
            raise HttpError(400, "bad content-length")
        if length > self.MAX_BODY:
            raise HttpError(413, "body too large")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "invalid json") from None
        if not isinstance(body, dict):
            raise HttpError(400, "expected a JSON object")
        return body

    def send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status, message):
        self.send_json(status, {"error": message})

    def check_bearer(self, token):
        """Require `Authorization: Bearer <token>`. An empty token turns the check off."""
        if not token:
            return
        expected = f"Bearer {token}".encode("utf-8")
        given = (self.headers.get("Authorization") or "").encode("utf-8")
        if not hmac.compare_digest(given, expected):
            raise HttpError(401, "unauthorized")
