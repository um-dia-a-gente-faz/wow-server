"""HTTP sidecar over navmesh.py (#82): POST /path, GET /height, GET /healthz. Stdlib only.

Run from tools/ as `python -m pathfinder.server`. Env: MMAPS_DIR (required, the TrinityCore mmaps/
directory), PATHFINDER_PORT (default 8770) and PATHFINDER_MAX_TILES (parsed tiles kept per map, default
64). The contract is in tools/pathfinder/README.md.
"""
import json
import logging
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .navmesh import DEFAULT_MAX_TILES, NavMesh, NavMeshError

LOG = logging.getLogger("pathfinder")
MAX_BODY = 1 << 16   # bytes; a path request is a few hundred


class BadRequest(Exception):
    pass


class Maps:
    """One NavMesh per map, loaded on first use and kept. A failed load (no .mmap) is not kept, so the
    next request tries again and finds the files once they appear. Each map keeps max_tiles parsed tiles."""

    def __init__(self, mmaps_dir: Path, max_tiles: int = DEFAULT_MAX_TILES):
        self.mmaps_dir = mmaps_dir
        self.max_tiles = max_tiles
        self._meshes: dict[int, NavMesh] = {}
        self._lock = threading.Lock()   # ponytail: one lock over all loads; per-map locks if a slow first load stalls others

    def get(self, map_id: int) -> NavMesh | None:
        mesh = self._meshes.get(map_id)          # a loaded map answers without the lock
        if mesh is not None:
            return mesh
        with self._lock:
            if map_id not in self._meshes:
                try:
                    mesh = NavMesh.load(self.mmaps_dir, map_id)
                except FileNotFoundError:
                    return None
                mesh.tiles.max_tiles = self.max_tiles   # load keeps its default; the server's cap wins
                self._meshes[map_id] = mesh
            return self._meshes[map_id]

    def loaded(self) -> list[int]:
        return sorted(list(self._meshes))         # list(dict) is atomic under the GIL


def _json_scalar(text: str, name: str):
    try:
        return json.loads(text)
    except ValueError:
        raise BadRequest(f"{name} must be a number") from None


def _required(query: dict[str, str], name: str) -> str:
    if name not in query:
        raise BadRequest(f"{name} is required")
    return query[name]


def _int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"{name} must be an integer")
    return value


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:                         # a huge integer has no float
        raise BadRequest(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise BadRequest(f"{name} must be a finite number")
    return number


def _point(obj, name: str) -> tuple[float, float, float]:
    if not isinstance(obj, dict):
        raise BadRequest(f"{name} must be an object with x, y and z")
    x, y, z = (_number(obj.get(k), f"{name}.{k}") for k in ("x", "y", "z"))
    return x, y, z


class Handler(BaseHTTPRequestHandler):
    timeout = 10   # socket timeout: a silent client is dropped after 10 s

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._answer(self._get)

    def do_POST(self):
        self._answer(self._post)

    def _answer(self, route):
        try:
            status, body = route()
        except BadRequest as err:
            status, body = 400, {"error": str(err)}
        except NavMeshError as err:
            status, body = 500, {"error": str(err)}
        except Exception:   # the last resort: log it, and answer JSON rather than drop the connection
            LOG.exception("unhandled error answering %s %s", self.command, self.path)
            status, body = 500, {"error": "internal error"}
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _get(self):
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/healthz":
            return 200, {"ok": True, "maps_loaded": self.server.maps.loaded()}
        if url.path == "/height":
            map_id = _int(_json_scalar(_required(query, "map"), "map"), "map")
            x = _number(_json_scalar(_required(query, "x"), "x"), "x")
            y = _number(_json_scalar(_required(query, "y"), "y"), "y")
            z = _number(_json_scalar(query["z"], "z"), "z") if "z" in query else None
            mesh = self.server.maps.get(map_id)
            return 200, {"z": None if mesh is None else mesh.height(x, y, z)}
        return 404, {"error": "not found"}

    def _post(self):
        if urlsplit(self.path).path != "/path":
            return 404, {"error": "not found"}
        body = self._body()
        map_id = _int(body.get("map"), "map")
        start = _point(body.get("from"), "from")
        end = _point(body.get("to"), "to")
        mesh = self.server.maps.get(map_id)
        if mesh is None:
            return 200, {"status": "no_map", "points": []}
        path = mesh.find_path(start, end)
        if path is None:
            return 200, {"status": "no_path", "points": []}
        return 200, {"status": "ok", "points": [{"x": x, "y": y, "z": z} for x, y, z in path]}

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad content-length") from None
        if not 0 <= length <= MAX_BODY:
            raise BadRequest("bad content-length")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise BadRequest("invalid json") from None
        if not isinstance(body, dict):
            raise BadRequest("expected a JSON object")
        return body


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], mmaps_dir: Path, max_tiles: int = DEFAULT_MAX_TILES):
        super().__init__(address, Handler)
        self.maps = Maps(mmaps_dir, max_tiles)


def main() -> None:
    mmaps_dir = os.environ.get("MMAPS_DIR")
    if not mmaps_dir:
        raise SystemExit("MMAPS_DIR is required: the TrinityCore mmaps/ directory")
    port = int(os.environ.get("PATHFINDER_PORT", "8770"))
    max_tiles = int(os.environ.get("PATHFINDER_MAX_TILES", DEFAULT_MAX_TILES))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    with Server(("", port), Path(mmaps_dir), max_tiles) as server:
        LOG.info("pathfinder on :%d, mmaps %s, max_tiles %d", port, mmaps_dir, max_tiles)
        server.serve_forever()


if __name__ == "__main__":
    main()
