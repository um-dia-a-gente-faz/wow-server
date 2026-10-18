#!/usr/bin/env python3
"""HTTP client for the navmesh sidecar (tools/pathfinder, #82): POST /path returns the waypoint
list the mover walks instead of a straight line (#312). stdlib only: agent/ never imports tools/.

The contract is the "HTTP contract" section of tools/pathfinder/README.md. Every failure the
caller must handle (network, timeout, non-200, malformed body) is one PathUnavailable; path()
raises nothing else, and never blocks longer than its timeout.
"""

import http.client
import json
import math
import threading
import urllib.error
import urllib.request

Point = tuple[float, float, float]


class PathUnavailable(Exception):
    """The sidecar gave no usable answer; `reason` says why (recorded as a path_fallback event)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PathClient:
    def __init__(self, base_url: str, timeout_s: float = 2.0):
        self.url = base_url.rstrip("/") + "/path"
        self.timeout_s = timeout_s

    def path(self, map_id: int, start: Point, end: Point) -> list[Point] | None:
        """Waypoints from `start` to `end` (WoW x, y, z; start first, end last), or None when the
        sidecar has no route (`no_path`) or no mesh for the map (`no_map`)."""
        body = json.dumps({"map": map_id,
                           "from": {"x": start[0], "y": start[1], "z": start[2]},
                           "to": {"x": end[0], "y": end[1], "z": end[2]}}).encode()
        # urlopen's timeout is per socket operation, so a slow trickle could outlast it; the
        # worker thread puts a hard deadline on the whole request.
        box: dict = {}

        def fetch():
            try:
                box["r"] = self._post(body)
            except PathUnavailable as e:
                box["err"] = e

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()
        thread.join(self.timeout_s)
        if thread.is_alive():
            raise PathUnavailable("timeout")
        if "err" in box:
            raise box["err"]
        if "r" not in box:  # the worker died on a bug: never let that escape as anything else
            raise PathUnavailable("internal error")
        return box["r"]

    def _post(self, body: bytes) -> list[Point] | None:
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                if resp.status != 200:
                    raise PathUnavailable(f"http {resp.status}")
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise PathUnavailable(f"http {e.code}") from None
        except (OSError, http.client.HTTPException) as e:
            raise PathUnavailable(str(e)) from None
        try:
            data = json.loads(raw)
            status = data["status"]
            if status in ("no_path", "no_map"):
                return None
            if status == "ok":
                points = [_point(p) for p in data["points"]]
                # A NaN or infinite waypoint never arrives, so the move would never end.
                if all(math.isfinite(c) for pt in points for c in pt):
                    return points
        except (ValueError, TypeError, KeyError):
            pass
        raise PathUnavailable("malformed response")


def _point(p) -> Point:
    """One {"x","y","z"} object as floats; a JSON boolean is not a number here."""
    values = (p["x"], p["y"], p["z"])
    if any(isinstance(v, bool) for v in values):
        raise TypeError("boolean coordinate")
    return (float(values[0]), float(values[1]), float(values[2]))
