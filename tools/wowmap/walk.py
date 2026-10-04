"""#178: "walk this agent here" from the console.

Two halves, like fleet.py:

* `build_runner_body` turns what the page sends (a click on a zone map, or a player's name)
  into the agent runner's `POST /agents/<name>/walk` body: world x/y on the zone's map, or
  `near_player`. The runner, and behind it the agent's own session, do the walking; this
  site never moves anything itself, and there is no teleport, GM command or database write
  anywhere on this path (CLAUDE.md, "Never").
* `WALK_CSS` / `WALK_JS` are the page half, spliced into app.PAGE: a block in the inspect
  drawer with "Walk to a point" (then click the map) and "Walk to a player" (then click a
  player in the list or on the map). It is disabled, with the reason, for a human character,
  an agent whose container is not running, one that is not logged in, and when no runner is
  configured.

Everything the runner returns is untrusted text and is rendered with textContent.
"""
import math

MAX_BODY_BYTES = 2048


class WalkError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _num(payload, key):
    v = payload.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise WalkError(400, f"{key} must be a finite number")
    return float(v)


def build_runner_body(payload, tables):
    """The page's walk request -> the runner's body. Raises WalkError(400/404)."""
    if not isinstance(payload, dict):
        raise WalkError(400, "body must be a JSON object")
    unknown = sorted(set(payload) - {"near_player", "area_id", "nx", "ny"})
    if unknown:
        raise WalkError(400, f"unknown field(s): {', '.join(unknown)}")
    if payload.get("near_player") is not None:
        if any(k in payload for k in ("area_id", "nx", "ny")):
            raise WalkError(400, "give either a map point or near_player, not both")
        name = payload["near_player"]
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 12 or not name.strip().isalpha():
            raise WalkError(400, "near_player must be a character name (2-12 letters)")
        return {"near_player": name.strip()}
    area = payload.get("area_id")
    if isinstance(area, bool) or not isinstance(area, int) or area <= 0:
        raise WalkError(400, "area_id must be a zone id")
    nx, ny = _num(payload, "nx"), _num(payload, "ny")
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        raise WalkError(400, "the point is outside the zone map")
    world = tables.from_normalised(area, nx, ny)
    map_id = tables.zone_map.get(area)
    if world is None or map_id is None:
        raise WalkError(404, "unknown zone")
    # z is left out on purpose: a map click carries no height, and the character keeps its own.
    return {"x": round(world[0], 2), "y": round(world[1], 2), "map": map_id}


WALK_CSS = r"""
  #walkbox { padding:8px 16px 0; font-size:12.5px; }
  #walkbox[hidden] { display:none; }
  #walkbox .wk-row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  #walkbox button { background:#141824; color:var(--fg); border:1px solid var(--line); border-radius:7px;
                    padding:4px 9px; font:inherit; cursor:pointer; }
  #walkbox button.on { background:#2b3550; border-color:#46557a; }
  #walkbox button[disabled] { opacity:.5; cursor:default; }
  #walkbox label { color:var(--dim); font-size:11.5px; display:inline-flex; gap:4px; align-items:center; }
  #walkbox .wk-why { color:var(--dim); font-size:12px; overflow-wrap:anywhere; }
  #walkbox .wk-hint { color:#f3d98b; margin-top:5px; }
  #walkbox .wk-res { margin-top:6px; border-left:3px solid var(--line); padding:2px 8px; overflow-wrap:anywhere;
                     white-space:pre-wrap; }
  #walkbox .wk-res.ok { border-color:#2f6b3a; }
  #walkbox .wk-res.bad { border-color:#d9534f; color:#ffb4b4; }
  #map.walking, #map.walking .leaflet-interactive { cursor:crosshair; }
"""

WALK_JS = r"""
<script>
// Walk mode (#178). Loaded before the main page script, so everything it needs from it
// (players, currentArea, mapCoordsText, ...) is read lazily, at event time.
// null when the character can be walked, else the reason it cannot. Pure: the unit
// under test is the rule, so the page and the tests read the same text.
function walkWhyNot(name, fleet, isAgent, here, area) {
  if (!isAgent) return 'Human characters cannot be moved from here: only an agent has a session that can walk, and GM commands are never used.';
  if (!fleet || !fleet.configured) return 'No agent runner is configured (AGENT_RUNNER_URL is empty), so the panel is read-only.';
  if (fleet.runner !== 'ok') return 'The agent runner is ' + fleet.runner + (fleet.error ? ' (' + fleet.error + ')' : '') + '.';
  const a = (fleet.agents || []).find((x) => x.name && x.name.toLowerCase() === name.toLowerCase());
  if (!a) return name + ' is not in the runner\'s roster.';
  if (a.retired) return name + ' is retired.';
  const cs = (a.container || {}).state || 'unknown';
  if (cs !== 'running') return name + '\'s container is ' + cs + (cs === 'unknown' ? ' (could not be determined)' : ', not running') + '. Start the agent first.';
  const api = a.agent_api || {};
  if (api.state !== 'ok') return name + '\'s own API is unreachable, so its state is unknown.';
  if (api.connected === false) return name + ' is not logged in to the world.';
  if (here && here.in_world === false) return name + ' is in an instance; walking cannot cross maps.';
  return null;
}

const Walk = (() => {
  const box = document.createElement('div');
  box.id = 'walkbox'; box.hidden = true;
  const drawer = document.getElementById('inspect');
  const fleetState = {data: null, at: 0};   // last /api/fleet answer, for the eligibility check
  let armed = null;        // null | 'point' | 'player'
  let target = null;       // the agent the armed mode will move
  let busy = false, dry = false, last = null, drawn = '';

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const agentEntry = (n) => (((fleetState.data && fleetState.data.agents) || [])
    .find((a) => a.name && a.name.toLowerCase() === n.toLowerCase()));

  function reason() {
    const n = Inspect.current();
    if (!n) return 'Open a character first.';
    const agent = AgentMind.agents().some((a) => a.toLowerCase() === n.toLowerCase()) || !!agentEntry(n);
    const here = (typeof players !== 'undefined') ? players.find((p) => p.name === n) : null;
    return walkWhyNot(n, fleetState.data, agent, here);
  }

  async function loadFleet() {
    try {
      const r = await fetch('/api/fleet', {cache: 'no-store'});
      if (r.ok) { fleetState.data = await r.json(); fleetState.at = Date.now(); }
    } catch (e) { /* keep the last answer; the reason text says what it knows */ }
    render();
  }

  function disarm() {
    armed = null; target = null;
    document.getElementById('map').classList.remove('walking');
    render();
  }
  function arm(mode) {
    if (busy) return;
    if (armed === mode) return disarm();
    if (reason()) return;
    if (mode === 'point' && typeof currentArea !== 'undefined' && currentArea && currentArea.continent_view) {
      last = {ok: false, text: 'Open the zone first: a continent map has no exact points to click.'};
      return render();
    }
    armed = mode; target = Inspect.current();
    document.getElementById('map').classList.toggle('walking', mode === 'point');
    render();
  }

  function fmt(p) { return p ? `${(+p.x).toFixed(1)}, ${(+p.y).toFixed(1)}` : '?'; }
  function describe(j, status) {
    if (j.outcome === 'dry_run') {
      return {ok: true, text: `Dry run, nothing moved. ${j.method || ''}\nfrom ${fmt(j.start)} to ${fmt(j.target)}, ${j.distance} yd`
        + (j.target && j.target.player ? ` (next to ${j.target.player})` : '')};
    }
    if (j.ok === true && j.outcome === 'arrived') {
      return {ok: true, text: `Arrived. ${fmt(j.start)} to ${fmt(j.end)}` + (j.end_distance != null ? `, ${j.end_distance} yd from the target` : '')
        + (j.elapsed_s != null ? `, ${j.elapsed_s}s` : '')};
    }
    // Anything else is a failure, with whatever the runner/agent said as the reason.
    let t = `${j.outcome || 'failed'}: ${j.error || 'HTTP ' + status}`;
    if (j.start && j.end && j.outcome !== 'refused') t += `\nmoved from ${fmt(j.start)} to ${fmt(j.end)}` + (j.end_distance != null ? `, ${j.end_distance} yd short` : '');
    if (j.detail) t += '\n' + j.detail;
    return {ok: false, text: t};
  }

  async function send(who, payload, label) {
    const verb = dry ? 'Dry run: plan the walk' : 'Walk';
    if (!confirm(`${verb} ${who} ${label}?\nIt walks with its own movement; it is not teleported.`)) return;
    busy = true; render();
    try {
      const r = await fetch(`/api/fleet/agents/${encodeURIComponent(who)}/walk` + (dry ? '?dry_run=1' : ''), {
        method: 'POST', headers: {'X-Fleet-Action': '1', 'Content-Type': 'application/json'},
        body: JSON.stringify(payload)});
      const j = await r.json().catch(() => ({error: 'unreadable answer (HTTP ' + r.status + ')'}));
      last = describe(j, r.status);
      last.text = new Date().toLocaleTimeString() + '  ' + who + ' ' + label + '\n' + last.text;
    } catch (e) {
      last = {ok: false, text: `wowmap did not answer (${e.message}); the walk may or may not have run`};
    }
    busy = false;
    disarm();
  }

  // Called by the page's map click handler. True when the click was consumed.
  function pickPoint(e) {
    if (armed !== 'point') return false;
    if (typeof currentArea === 'undefined' || !currentArea || currentArea.continent_view) { disarm(); return true; }
    const who = target, c = calibration();
    const pt = eventPoint(e.originalEvent);
    const nx = (pt.x - c.dx) / MAP_FRAME_W, ny = (pt.y - c.dy) / MAP_FRAME_H;
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return true;       // outside the art: keep waiting
    const g = [(nx * 100).toFixed(1), (ny * 100).toFixed(1)];
    send(who, {area_id: currentArea.area_id, nx, ny}, `to ${g[0]}, ${g[1]} in ${currentArea.name}`);
    return true;
  }
  // Called when a character is chosen (list row, marker, chat sender). True when consumed.
  function pickPlayer(name) {
    if (armed !== 'player') return false;
    if (name.toLowerCase() === (target || '').toLowerCase()) { last = {ok: false, text: 'Pick a player other than the agent itself.'}; render(); return true; }
    send(target, {near_player: name}, 'next to ' + name);
    return true;
  }

  function render() {
    const n = Inspect.current();
    box.hidden = !n || !drawer.classList.contains('open');
    if (box.hidden) return;
    const why = reason();
    // Rebuilding the box under the pointer would swallow a click, so only do it on a change.
    const sig = JSON.stringify([n, why, armed, target, busy, dry, last]);
    if (sig === drawn) return;
    drawn = sig;
    const row = el('div', 'wk-row');
    const bp = el('button', armed === 'point' ? 'on' : null, 'Walk to a point');
    const bq = el('button', armed === 'player' ? 'on' : null, 'Walk to a player');
    bp.disabled = bq.disabled = !!why || busy;
    bp.title = 'Then click the map. The agent walks there with its own movement.';
    bq.title = 'Then click a player in the list or on the map.';
    bp.onclick = () => arm('point');
    bq.onclick = () => arm('player');
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = dry; cb.onchange = () => { dry = cb.checked; };
    const lab = el('label', null); lab.append(cb, 'dry run (plan only)');
    lab.title = 'Resolve the target and show the plan without moving anything';
    row.append(bp, bq, lab);
    const parts = [row];
    if (why) parts.push(el('div', 'wk-why', why));
    if (busy) parts.push(el('div', 'wk-hint', 'Walking… this can take up to two minutes.'));
    else if (armed === 'point') parts.push(el('div', 'wk-hint', `Click a point on the map for ${target} (Esc to cancel).`));
    else if (armed === 'player') parts.push(el('div', 'wk-hint', `Click a player in the list or on the map for ${target} to walk next to (Esc to cancel).`));
    if (last) parts.push(el('div', 'wk-res ' + (last.ok ? 'ok' : 'bad'), last.text));
    box.replaceChildren(...parts);
  }

  // Sits between the drawer head and its body; the head is rebuilt by Inspect, this is not.
  drawer.insertBefore(box, document.getElementById('inspect-body'));
  addEventListener('keydown', (e) => { if (e.key === 'Escape' && armed) disarm(); });
  let shownFor = null;
  setInterval(() => {
    const n = Inspect.current();
    if (n !== shownFor) { shownFor = n; if (armed && n !== target) disarm(); loadFleet(); }
    render();
  }, 500);
  setInterval(() => { if (Inspect.current() && !document.hidden) loadFleet(); }, 10000);
  return {pickPoint, pickPlayer, active: () => armed !== null};
})();
</script>
"""
