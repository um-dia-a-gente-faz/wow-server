"""POST JSON, read JSON, over stdlib `urllib` (#454). The one HTTP transport
behind agent/jev.py and agent/llm.py.

What it owns: building the request, the timeout, mapping every failure class
(HTTP status, network error, a timeout or dropped connection while reading the
body, non-JSON body) to one `HttpJsonError`, the `Retry-After` reading, an
optional in-call retry, and redaction. Callers decide what a failure means
(`JevError`, `LLMUnavailable`) and how their messages read.

Redaction: the API key travels in the `Authorization` header, and a provider
may echo it in an error body or take it in the URL query. Every string this
module puts in an exception or a log line goes through `redact()`, and the
exception is raised `from None` so the original `HTTPError`/`URLError` (whose
repr and traceback carry the raw URL) never reaches a log either.

Retry defaults to none (neither caller retries in-call today; the LLM client
cools a model down instead). A caller that wants it passes `retries`,
`retry_statuses` and a `sleep`; the wait is the server's `Retry-After` when it
sent one, else `backoff_s`, never more than `max_wait_s`.
"""

import email.utils
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping

log = logging.getLogger("agent.httpjson")

REDACTED = "[redacted]"
_SENSITIVE_QUERY = frozenset({"key", "api_key", "apikey", "api-key", "token", "access_token",
                              "auth", "authorization", "secret", "password"})
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization|x-api-key|api-key)(['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+")
_QUERY_PARAM_RE = re.compile(r"([?&])([A-Za-z0-9_-]+)=[^&\s'\"]*")


class HttpJsonError(Exception):
    """A request failed. `kind` is "http" (the server answered with an error
    status), "network" (no usable answer) or "non_json" (2xx with a body that
    is not JSON, or not an object when `require_object`). `url` and `body` are
    already redacted; `body` is the response/error body (None when unreadable),
    `detail` the network reason. `retry_after` is seconds, when advertised."""

    def __init__(self, kind: str, url: str, *, status: int | None = None, detail: str = "",
                 body: bytes | None = None, retry_after: float | None = None):
        super().__init__(f"{kind} error from {url}" + (f" (HTTP {status})" if status else ""))
        self.kind = kind
        self.url = url
        self.status = status
        self.detail = detail
        self.body = body
        self.retry_after = retry_after


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every secret value, bearer token, authorization header value
    and sensitive URL query parameter replaced by [redacted]."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)

    def fix(m: re.Match) -> str:
        if m.group(2).lower() not in _SENSITIVE_QUERY:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}={REDACTED}"
    return _QUERY_PARAM_RE.sub(fix, text)


_SOONEST_RESET_RE = re.compile(
    r"reset\D{0,10}?(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hours?)?\b",
    re.IGNORECASE)


def retry_after_seconds(header: str | None, body: str) -> float | None:
    """Seconds until the server says to retry: the Retry-After header
    (delta-seconds or an HTTP date), else FreeLLMAPI's "Soonest reset ~82s"
    hint in the error message. None if neither is present/parseable. A unit
    other than seconds is accepted defensively; no unit means seconds."""
    if header:
        header = header.strip()
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(header)
                return max(0.0, when.timestamp() - time.time())
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    match = _SOONEST_RESET_RE.search(body or "")
    if match is None:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit == "ms" or unit.startswith("milli"):
        return value / 1000.0
    if unit.startswith("h"):
        return value * 3600.0
    if unit.startswith("m"):
        return value * 60.0
    return value


def _secrets(headers: Mapping[str, str]) -> list[str]:
    out = []
    for name, value in headers.items():
        if name.lower() == "content-type":
            continue
        out.append(value)
        parts = value.split(None, 1)
        if len(parts) == 2:   # "Bearer <token>": the token alone may be echoed
            out.append(parts[1])
    return out


def _safe_body(body: bytes | None, secrets: list[str]) -> bytes | None:
    return None if body is None else redact(body.decode("utf-8", "replace"), secrets).encode()


def _attempt(url: str, data: bytes, headers: Mapping[str, str], timeout: float,
             error_body_limit: int | None, secrets: list[str]) -> bytes:
    safe_url = redact(url, secrets)
    req = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body: bytes | None
        try:
            body = e.read() if e.fp is not None else b""
        except (OSError, http.client.HTTPException):
            body = None
        # The retry hint reads the first `error_body_limit` raw bytes, as before.
        # Redaction runs on the whole body and the cut comes after, so a key
        # straddling the limit cannot leave a prefix behind.
        hint = body if error_body_limit is None or body is None else body[:error_body_limit]
        header = e.headers.get("Retry-After") if e.headers is not None else None
        retry_after = retry_after_seconds(header, (hint or b"").decode("utf-8", "replace"))
        safe = _safe_body(body, secrets)
        if safe is not None and error_body_limit is not None:
            safe = safe[:error_body_limit]
        raise HttpJsonError("http", safe_url, status=e.code, body=safe,
                            retry_after=retry_after) from None
    except urllib.error.URLError as e:
        raise HttpJsonError("network", safe_url, detail=redact(str(e.reason), secrets)) from None
    except (OSError, http.client.HTTPException) as e:
        # urllib wraps OSError as URLError only while sending. A timeout or a
        # dropped connection while reading the body surfaces bare (UM-81).
        raise HttpJsonError("network", safe_url, detail=redact(str(e), secrets)) from None


def post_json(url: str, body: object, *, api_key: str = "", timeout: float = 20.0,
              json_default: Callable[[object], object] | None = None,
              require_object: bool = False, error_body_limit: int | None = None,
              retries: int = 0, retry_statuses: Iterable[int] = (), backoff_s: float = 0.0,
              max_wait_s: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> object:
    """POST `body` as JSON to `url` and return the decoded JSON reply.

    Raises HttpJsonError, never anything else a request can throw.
    `json_default` is json.dumps' `default`; `require_object` makes a reply that
    is not a JSON object a "non_json" error. `error_body_limit` caps the error
    body kept on the exception: the body is redacted in full and then cut to
    that many bytes, and the retry hint is read from its first
    `error_body_limit` raw bytes. Up to `retries` further attempts follow a
    reply whose status is in `retry_statuses`, after `sleep(wait)`."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    secrets = _secrets(headers)
    data = json.dumps(body, default=json_default).encode("utf-8")
    statuses = frozenset(retry_statuses)
    attempt = 0
    while True:
        try:
            raw = _attempt(url, data, headers, timeout, error_body_limit, secrets)
            break
        except HttpJsonError as e:
            if e.kind != "http" or e.status not in statuses or attempt >= retries:
                raise
            wait = min(max(e.retry_after if e.retry_after is not None else backoff_s, 0.0), max_wait_s)
            attempt += 1
            log.warning("HTTP %s from %s; retry %d/%d in %.1fs", e.status, e.url, attempt, retries, wait)
            sleep(wait)
    safe_url = redact(url, secrets)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        raise HttpJsonError("non_json", safe_url, body=_safe_body(raw, secrets)) from None
    if require_object and not isinstance(decoded, dict):
        raise HttpJsonError("non_json", safe_url, detail="not a JSON object", body=_safe_body(raw, secrets))
    return decoded
