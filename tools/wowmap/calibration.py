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


def load_calibrations():
    """Load the small operator-maintained per-zone pixel-offset store."""
    try:
        with open(CALIBRATION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top-level value must be an object")
        return {
            str(area_id): {"dx": float(value.get("dx", 0)), "dy": float(value.get("dy", 0))}
            for area_id, value in data.items()
            if isinstance(value, dict)
        }
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        state.log.warning("could not load calibration store %s: %s", CALIBRATION_FILE, exc)
        return {}


calibrations = load_calibrations()


def save_calibration(area_id, dx, dy):
    """Read-modify-write one zone's offset; callers receive the saved value."""
    value = {"dx": round(dx, 2), "dy": round(dy, 2)}
    with _lock:
        calibrations[str(area_id)] = value
        directory = os.path.dirname(CALIBRATION_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{CALIBRATION_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(calibrations, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(temporary, CALIBRATION_FILE)
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
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}
    except OSError as exc:
        state.log.exception("could not persist calibration")
        return 500, {"error": str(exc)}
