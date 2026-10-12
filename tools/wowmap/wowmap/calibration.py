"""Per-zone pixel offsets for the map markers: a small operator-maintained JSON store,
edited from the page's calibration mode (POST /api/calibrate)."""
import json
import math
import os
import threading

import state

CALIBRATION_FILE = os.environ.get(
    "CALIBRATION_FILE", os.path.join(os.path.dirname(__file__), "calibration.json")
)
MAX_CALIBRATION_PAYLOAD_BYTES = 65536
_lock = threading.Lock()


class CalibrationUnreadable(ValueError):
    """calibration.json exists but is not a readable store; saving would overwrite it (#608)."""


def _read_store():
    """The zones in calibration.json ({} if the file is missing).

    Raises CalibrationUnreadable when the file exists but is not valid JSON, not an object,
    has a zone that is not an object, or has an offset that does not parse as a number."""
    try:
        with open(CALIBRATION_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: top-level value must be an object")
    store = {}
    for area_id, value in data.items():
        if not isinstance(value, dict):
            raise CalibrationUnreadable(f"{CALIBRATION_FILE} is unreadable: zone {area_id} is not an object")
        try:
            store[str(area_id)] = {"dx": float(value.get("dx", 0)), "dy": float(value.get("dy", 0))}
        except (TypeError, ValueError) as exc:
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


def save_calibration(area_id, dx, dy):
    """Read-modify-write one zone's offset; callers receive the saved value.

    The file is re-read on every save, so other zones survive and an unreadable file is
    refused (CalibrationUnreadable) with its bytes untouched until someone fixes it."""
    value = {"dx": round(dx, 2), "dy": round(dy, 2)}
    with _lock:
        store = _read_store()
        store[str(area_id)] = value
        directory = os.path.dirname(CALIBRATION_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{CALIBRATION_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(temporary, CALIBRATION_FILE)
        calibrations.clear()
        calibrations.update(store)
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
