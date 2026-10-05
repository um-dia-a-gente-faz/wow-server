"""Fleet glue: the agents' read-only observability APIs (UM-50), the runner behind the
fleet panel (#137) and the walk-to-point action (#178)."""
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import fleet
import state
import walk
from webio import NO_STORE


def parse_agent_urls(spec):
    """AGENT_API_URLS -> {lowercased name: (name, base url)}. Malformed
    entries are skipped; only http(s) URLs are accepted."""
    out = {}
    for part in (spec or "").split(","):
        name, sep, url = part.strip().partition("=")
        name, url = name.strip(), url.strip().rstrip("/")
        if sep and name and url.startswith(("http://", "https://")):
            out[name.lower()] = (name, url)
    return out


# UM-50: agents' read-only observability APIs. wowmap proxies them so the page
# needs no extra ports or CORS; only these GET views are ever forwarded.
AGENT_APIS = parse_agent_urls(os.environ.get("AGENT_API_URLS", ""))
AGENT_VIEWS = ("healthz", "state", "perception", "brain")
AGENT_PROXY_TIMEOUT_S = 3
MAX_AGENT_RESPONSE_BYTES = 4 * 1024 * 1024

# #137: the agent runner behind the fleet panel. URL and token are read here once and
# only ever used by fleet.Runner; no handler echoes either.
RUNNER = fleet.Runner(os.environ.get("AGENT_RUNNER_URL", ""), os.environ.get("AGENT_RUNNER_TOKEN", ""))


def fleet_status():
    """GET /api/fleet. Without a runner the panel is read-only: the rows are the
    agents in AGENT_API_URLS with only their own /healthz probed, everything else
    stays out of the row so the page shows it as unknown."""
    out = RUNNER.status()
    if RUNNER.configured:
        return out

    def probe(entry):
        name, _ = entry
        status, body = fetch_agent_view(name, "healthz")
        try:
            h = json.loads(body) if status == 200 else {}
        except ValueError:
            h = {}
        ok = isinstance(h, dict) and h.get("ok") is True
        return {"name": name, "agent_api": {"state": "ok" if ok else "unreachable",
                                            "connected": h.get("connected") if ok else None}}
    entries = sorted(AGENT_APIS.values())
    if entries:
        with ThreadPoolExecutor(max_workers=8) as pool:
            out["agents"] = list(pool.map(probe, entries))
    return out


def fetch_agent_view(name, view, n=None):
    """GET <agent base>/<view> and return (status, body bytes). Unknown agent
    or view -> 404; an unreachable agent -> 502. The agent's URL is never
    echoed back to the browser."""
    entry = AGENT_APIS.get((name or "").lower())
    if entry is None or view not in AGENT_VIEWS:
        return 404, json.dumps({"error": "unknown agent or view"}).encode()
    url = f"{entry[1]}/{view}"
    if view == "brain" and n is not None:
        url += f"?n={int(n)}"
    try:
        with urllib.request.urlopen(url, timeout=AGENT_PROXY_TIMEOUT_S) as r:
            return r.status, r.read(MAX_AGENT_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        return 502, json.dumps({"error": f"agent API returned {e.code}"}).encode()
    except (OSError, ValueError) as e:
        state.log.info("agent API %s unreachable: %s", entry[0], e)
        return 502, json.dumps({"error": "agent API unreachable"}).encode()


def fleet_action(req, name, action):
    """POST /api/fleet/agents/<name>/<action>: forwarded to the runner."""
    # A cross-site form can POST here, but it cannot set a custom header without a
    # CORS preflight, which wowmap never answers: so the header proves it is our page.
    if req.header("X-Fleet-Action") != "1":
        return 403, {"error": "missing X-Fleet-Action header"}
    if action == "walk":
        return fleet_walk(req, name)
    status, body = RUNNER.act(name, action)
    return status, body, "application/json", NO_STORE


def fleet_walk(req, name):
    """#178: translate the page's click into world coordinates and hand it to the runner."""
    try:
        length = int(req.header("Content-Length", "0"))
        if not 0 < length <= walk.MAX_BODY_BYTES:
            raise walk.WalkError(400, "a JSON object body is required")
        try:
            payload = json.loads(req.read_body(length))
        except ValueError:
            raise walk.WalkError(400, "body is not valid JSON")
        body = walk.build_runner_body(payload, state.tables())
    except walk.WalkError as e:
        return (e.status, {"ok": False, "outcome": "refused", "code": "bad_request", "error": str(e)},
                "application/json", NO_STORE)
    dry = req.qs1("dry_run", "") in ("1", "true")
    status, out = RUNNER.walk(name, body, dry_run=dry, operator=req.client_ip)
    return status, out, "application/json", NO_STORE
