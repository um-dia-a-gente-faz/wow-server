"""Socket-free request/response types shared by the route table and its handlers.

A handler takes a Request and returns `(status, body)` or `(status, body, ctype, cache)`;
body is a dict/list (sent as JSON), str or bytes. app.Handler is the only code that
touches a socket.
"""
from typing import Callable, Mapping, NamedTuple

NO_STORE = "no-store"


class Request(NamedTuple):
    method: str
    path: str                                   # decoded-as-received, query removed
    qs: Mapping[str, list]                      # parse_qs result
    headers: Mapping[str, str]
    read_body: Callable[[int], bytes]           # read_body(length) -> bytes
    client_ip: str = ""

    def header(self, name, default=None):
        return self.headers.get(name, default) if self.headers else default

    def qs1(self, name, default=None):
        return self.qs.get(name, [default])[0]


def full(resp):
    """(status, body, ctype, cache) from a handler's 2- or 4-tuple."""
    status, body, *rest = resp
    ctype = rest[0] if rest else "application/json"
    cache = rest[1] if len(rest) > 1 else None
    return status, body, ctype, cache


def not_found():
    return 404, {"error": "not found"}
