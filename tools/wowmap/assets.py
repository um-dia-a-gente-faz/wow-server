"""Static files: extracted zone art (/maps), item icons (/icons), vendored Leaflet (/static)."""
import os
import re

import state
from fogview import fog_image
from webio import not_found

# Vendored front-end files (Leaflet), served under /static/. An explicit list, so
# nothing else in the directory is ever reachable.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {"leaflet.js": "text/javascript; charset=utf-8",
                "leaflet.css": "text/css; charset=utf-8"}
# What item_icons.icon_file() produces; anything else under /icons/ is a 404.
ICON_FILE_RE = re.compile(r"[a-z0-9_\-]+\.png")


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def maps(req, rest):
    fn = os.path.basename(rest)
    if "explored" in req.qs and fn.endswith(".png") and fn[:-4].isdigit():
        # The same zone art, with only the listed overlays revealed.
        body = fog_image(int(fn[:-4]), req.qs["explored"][0])
        if body is None:
            return not_found()
        return 200, body, "image/png", "max-age=86400"
    fp = os.path.join(state.MAPS_DIR, fn)
    if os.path.isfile(fp) and fn.endswith(".png"):
        return 200, _read(fp), "image/png", "max-age=86400"
    return not_found()


def static(req, fn):
    if fn in STATIC_FILES:
        return 200, _read(os.path.join(STATIC_DIR, fn)), STATIC_FILES[fn], "public, max-age=86400"
    return not_found()


def icons(req, fn):
    fp = os.path.join(state.ICONS_DIR, fn)
    if ICON_FILE_RE.fullmatch(fn) and os.path.isfile(fp):
        # Icons never change for a given client build.
        return 200, _read(fp), "image/png", "public, max-age=2592000, immutable"
    return not_found()
