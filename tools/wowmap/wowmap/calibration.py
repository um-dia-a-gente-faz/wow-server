"""Per-zone pixel offsets for the map markers: a small operator-maintained JSON store,
edited from the page's calibration mode (POST /api/calibrate)."""
import contextlib
import json
import math
import os
import secrets
import stat
import threading

import state

CALIBRATION_FILE = os.environ.get(
    "CALIBRATION_FILE", os.path.join(os.path.dirname(__file__), "calibration.json")
)
MAX_CALIBRATION_PAYLOAD_BYTES = 65536
JSON_WHITESPACE = " \t\r\n"  # the whitespace JSON allows; an all-whitespace file is an empty store
_lock = threading.Lock()
# The writer never writes a "schema" key while this is 1: the site deployed before #641 reads a
# top-level "schema": 1 as a zone that is not an object and would refuse the file after a
# rollback. An absent schema already means 1 (ADR 0018), so the key is not needed yet.
CALIBRATION_SCHEMA = 1


class CalibrationUnreadable(ValueError):
    """calibration.json exists but is not a readable store; saving would overwrite it (#608)."""


class CalibrationNewer(CalibrationUnreadable):
    """calibration.json was written by a newer site; this one neither reads nor overwrites it (ADR 0018)."""


def _read_store():
    """The zones in calibration.json ({} if the file is missing, empty or only whitespace).

    Raises CalibrationUnreadable when the file exists but is not valid JSON, not an object,
    has a zone that is not an object, has an offset that does not parse as a number, or has a
    schema that is not a positive integer. Raises CalibrationNewer when the schema is above
    CALIBRATION_SCHEMA. An absent schema is version 1."""
    try:
        with open(CALIBRATION_FILE, encoding="utf-8") as f:
            text = f.read()
        if not text.strip(JSON_WHITESPACE):
            return {}
        data = json.loads(text)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:
        raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: top-level value must be an object")
    schema = data.pop("schema", 1)
    if type(schema) is not int or schema < 1:  # type(): a bool or a float is not a schema number
        raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: schema must be a positive integer")
    if schema > CALIBRATION_SCHEMA:
        raise CalibrationNewer(
            f"{CALIBRATION_FILE} is schema {schema}, newer than {CALIBRATION_SCHEMA}: "
            "not read, not overwritten; deploy the newer site")
    store = {}
    for area_id, value in data.items():
        if not isinstance(value, dict):
            raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: zone {area_id} is not an object")
        try:
            store[str(area_id)] = {"dx": float(value.get("dx", 0)), "dy": float(value.get("dy", 0))}
        except (TypeError, ValueError, OverflowError) as exc:
            raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: zone {area_id}: {exc}") from exc
    return store


def load_calibrations():
    """Load the small operator-maintained per-zone pixel-offset store; never raises."""
    try:
        return _read_store()
    except CalibrationUnreadable as exc:
        state.log.warning("could not load calibration store %s: %s", CALIBRATION_FILE, exc)
        return {}


calibrations = load_calibrations()


def _write_atomic(path, text):
    """Write `text` to `path` through a unique temporary file in the same directory: flush, fsync,
    os.replace. On any failure the temporary file is removed, the target is untouched and the
    original exception propagates. The temporary is created with os.open(0o666), so a new target
    gets the mode open(..., "w") gave it (0o666 minus the umask, applied by the kernel per call);
    an existing target keeps its mode through fchmod. No directory fsync (ADR 0018).
    Private copy: agent/persist.py, tools/ and scripts/ each carry one until tools/common does."""
    path = os.fspath(path)
    directory, name = os.path.split(path)
    tmp = os.path.join(directory or ".", f".{name}.{secrets.token_hex(8)}.tmp")
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
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


def save_calibration(area_id, dx, dy):
    """Read-modify-write one zone's offset; callers receive the saved value.

    The file is re-read on every save, so other zones survive and an unreadable file is
    refused (CalibrationUnreadable) with its bytes untouched until someone fixes it.
    "schema" is the file's version key, not a zone: it is refused (ValueError) before any I/O."""
    if str(area_id) == "schema":
        raise ValueError('"schema" is the file version key, not a zone')
    value = {"dx": round(dx, 2), "dy": round(dy, 2)}
    with _lock:
        store = _read_store()
        store[str(area_id)] = value
        directory = os.path.dirname(CALIBRATION_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _write_atomic(CALIBRATION_FILE, json.dumps(store, indent=2, sort_keys=True) + "\n")
        calibrations.update(store)
        for stale in set(calibrations) - set(store):
            del calibrations[stale]
    return value


def calibrate(req):
    """POST /api/calibrate -> (status, body)."""
    try:
        length = int(req.header("Content-Length", "0"))
        if length > MAX_CALIBRATION_PAYLOAD_BYTES:
            return 413, {"error": "payload too large"}
        payload = json.loads(req.read_body(length))
        area_id = int(payload["area_id"])
        dx, dy = float(payload["dx"]), float(payload["dy"])
        if area_id <= 0 or not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError("area_id must be positive and offsets must be finite")
        value = save_calibration(area_id, dx, dy)
        state.log.info("saved calibration for area %d: dx=%s dy=%s", area_id, value["dx"], value["dy"])
        return 200, {"area_id": area_id, **value}
    except CalibrationUnreadable as exc:
        state.log.warning("calibration not saved: %s", exc)
        return 500, {"error": str(exc)}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}
    except OSError as exc:
        state.log.exception("could not persist calibration")
        return 500, {"error": str(exc)}
