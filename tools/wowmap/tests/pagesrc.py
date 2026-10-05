"""The page as the browser gets it, for tests that assert on its source (#262).

PAGE is static/index.html (chat feed URL filled in) followed by every css/js file it
references, in load order, so a test can search "the page" without caring which file
a function lives in. The per-panel names are the single files.
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pages  # noqa: E402

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def referenced(html=None):
    """Local /static/ files the page links, in document order."""
    return re.findall(r'(?:href|src)="/static/([^"]+)"', pages.PAGE if html is None else html)


PAGE = pages.PAGE + "".join("\n" + read(n) for n in referenced())
ACTIVITY_JS, ACTIVITY_CSS, AGENT_JS = read("activity.js"), read("activity.css"), read("agent.js")
INSPECT_JS = read("format.js") + read("inventory.js") + read("character.js")
FLEET_JS, WALK_JS = read("fleet.js"), read("walk.js")
