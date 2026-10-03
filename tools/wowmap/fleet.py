"""#137: the agent fleet panel — wowmap's proxy to the agent runner (#136).

The runner (tools/agent-runner) owns the Docker socket and the token; wowmap
only forwards. The browser talks to wowmap, wowmap talks to the runner, and the
bearer token (AGENT_RUNNER_TOKEN) is added here, server-side, and scrubbed from
everything that is handed back. Whatever the runner says about a failure is
passed on verbatim, never replaced with a generic "done".

Runner contract (#136):
    GET  /agents                  {"agents": [...], "image": {...}, "generated_at": ...}
    GET  /agents/<name>           one entry
    POST /agents/<name>/start     {"action", "built"?, "agent": entry} or {"error", "detail"}
    POST /agents/<name>/stop      same
An agent entry keeps its signals apart: container.state (running|exited|absent|unknown),
agent_api.state (ok|unreachable) + connected, audit.{last_ts,age_seconds},
brain.{model,valid,last_error}, character.{level,playtime_seconds,zone,source}.

The page half (FLEET_CSS / FLEET_HTML / FLEET_JS) is spliced into app.PAGE.
"""
import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import quote

log = logging.getLogger("wowmap.fleet")

STATUS_TIMEOUT_S = 10          # the runner asks docker and every agent's API
ACTION_TIMEOUT_S = 930         # the runner's own build timeout is 900 s
MAX_RESPONSE_BYTES = 1024 * 1024
NAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,32}$")
ACTIONS = ("start", "stop")
REDACTED = "[redacted]"


class Runner:
    """Client for one agent runner. `url` empty means none is configured."""

    def __init__(self, url, token):
        self.url = (url or "").strip().rstrip("/")
        self.token = token or ""
        self._roster = []    # {name, account} from the last good answer, so a dead runner still lists rows

    @property
    def configured(self):
        return self.url.startswith(("http://", "https://"))

    def _scrub(self, text):
        return text.replace(self.token, REDACTED) if self.token else text

    def _request(self, method, path, timeout):
        """-> (http status, parsed JSON dict). Raises OSError (unreachable) or
        ValueError (not a JSON object). HTTP errors come back as a status."""
        req = urllib.request.Request(self.url + path, method=method,
                                     data=b"" if method == "POST" else None)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, raw = r.status, r.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as e:
            with e:
                status, raw = e.code, e.read(MAX_RESPONSE_BYTES)
        body = json.loads(self._scrub(raw.decode("utf-8", "replace")))
        if not isinstance(body, dict):
            raise ValueError("runner answered with something other than a JSON object")
        return status, body

    def status(self):
        """The fleet as the page needs it. Never raises: a runner that cannot be
        asked is a state. `agents` then only names the agents the runner last listed
        (no signals), so the page can show each row as unknown instead of nothing."""
        out = {"configured": self.configured, "controls": self.configured,
               "runner": "disabled", "error": None, "agents": [], "image": None,
               "generated_at": None}
        if not self.configured:
            out["error"] = "AGENT_RUNNER_URL is not set"
            return out
        try:
            code, body = self._request("GET", "/agents", STATUS_TIMEOUT_S)
        except (OSError, ValueError) as e:
            log.info("agent runner unreachable: %s", e)
            out.update(runner="unreachable", error="the agent runner did not answer",
                       agents=list(self._roster))
            return out
        if code in (401, 403):
            out.update(runner="unauthorized", agents=list(self._roster),
                       error="the agent runner rejected wowmap's token (check AGENT_RUNNER_TOKEN)")
        elif code != 200 or not isinstance(body.get("agents"), list):
            out.update(runner="error", agents=list(self._roster),
                       error=self._scrub(str(body.get("error") or f"runner returned HTTP {code}"))[:300])
        else:
            out.update(runner="ok", agents=body["agents"], image=body.get("image"),
                       generated_at=body.get("generated_at"))
            self._roster = [{"name": a["name"], "account": a.get("account")}
                            for a in body["agents"] if isinstance(a, dict) and a.get("name")]
        return out

    def act(self, name, action):
        """POST start|stop -> (http status for the browser, body). The runner's
        own error text and detail pass through; its 401 does not: that is wowmap's
        misconfiguration, not something the browser's user can fix."""
        if action not in ACTIONS or not NAME_RE.match(name or ""):
            return 404, {"error": "not found"}
        if not self.configured:
            return 503, {"error": "no agent runner is configured (AGENT_RUNNER_URL is empty); the panel is read-only"}
        try:
            code, body = self._request("POST", f"/agents/{quote(name, safe='')}/{action}", ACTION_TIMEOUT_S)
        except (OSError, ValueError) as e:
            log.warning("agent runner %s %s failed: %s", action, name, e)
            return 502, {"error": "the agent runner did not answer; the action may or may not have run"}
        if code in (401, 403):
            return 502, {"error": "the agent runner rejected wowmap's token (check AGENT_RUNNER_TOKEN)"}
        log.info("fleet %s %s -> runner HTTP %d", action, name, code)
        return code, body


# ---------------------------------------------------------------- page: fleet panel
# Every string from the runner (character names, gateway error text, docker
# stderr) is untrusted and rendered with textContent, never innerHTML.
FLEET_CSS = r"""
  #fleet { position:absolute; inset:0; z-index:1200; background:var(--bg); overflow:auto;
           padding:14px 16px 24px; }
  #fleet[hidden] { display:none; }
  #fleet h2 { font-size:15px; margin:0 0 10px; display:flex; gap:10px; align-items:baseline; }
  #fleet h2 small { color:var(--dim); font-weight:normal; font-size:12px; }
  .fl-banner { border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:0 0 12px;
               background:#141824; overflow-wrap:anywhere; }
  .fl-banner.bad { border-color:#8a3b3b; background:#2a1517; color:#ffb4b4; }
  .fl-banner.warn { border-color:#7a6430; background:#2a2412; color:#f3d98b; }
  .fl-totals { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; }
  .fl-totals .stat { flex:0 0 120px; }
  .fl-totals .stat.attn b { color:#ff8b8b; }
  .fl-bar { display:flex; gap:6px; align-items:center; margin:0 0 10px; flex-wrap:wrap; }
  .fl-bar .sp { flex:1; }
  .fl-bar .meta { color:var(--dim); font-size:12px; }
  table.fl { border-collapse:collapse; width:100%; font-size:13px; }
  table.fl th { text-align:left; color:var(--dim); font-weight:600; font-size:11px; letter-spacing:.3px;
                text-transform:uppercase; padding:6px 8px; border-bottom:1px solid var(--line);
                position:sticky; top:-14px; background:var(--bg); }
  table.fl td { padding:7px 8px; border-bottom:1px solid #20263a; vertical-align:top; overflow-wrap:anywhere; }
  table.fl tr.attn td:first-child { box-shadow:inset 3px 0 0 #d9534f; }
  table.fl tr.stopped td { opacity:.62; }
  table.fl tr.stopped td:first-child, table.fl tr.stopped td:last-child { opacity:1; }
  table.fl .nm { font-weight:600; }
  table.fl .sub { color:var(--dim); font-size:11.5px; }
  .fl-b { display:inline-block; border-radius:999px; padding:1px 8px; font-size:11.5px; font-weight:600;
          border:1px solid var(--line); background:#141824; color:var(--dim); }
  .fl-b.ok { color:#7ddf8a; border-color:#2f6b3a; }
  .fl-b.warn { color:#f3d98b; border-color:#7a6430; }
  .fl-b.bad { color:#ff8b8b; border-color:#8a3b3b; }
  .fl-b.off { color:var(--dim); border-style:dashed; }
  .fl-b.unk { color:#c9a6ff; border-color:#5b4580; border-style:dotted; }
  .fl-err { color:#ff8b8b; font-size:12px; margin-top:3px; }
  .fl-act button { padding:3px 9px; font-size:12px; }
  .fl-act button[disabled] { opacity:.5; cursor:default; }
  .fl-log { margin-top:16px; }
  .fl-log h3 { font-size:12px; color:var(--dim); margin:0 0 6px; text-transform:uppercase; letter-spacing:.3px; }
  .fl-log .row { border-left:3px solid var(--line); padding:3px 9px; margin:0 0 5px; font-size:12.5px;
                 overflow-wrap:anywhere; white-space:pre-wrap; }
  .fl-log .row.ok { border-color:#2f6b3a; }
  .fl-log .row.bad { border-color:#d9534f; color:#ffb4b4; }
  .fl-note { color:var(--dim); font-size:12px; margin-top:14px; border-top:1px dashed var(--line); padding-top:8px; }
"""

FLEET_HTML = r"""
<section id="fleet" hidden aria-label="Agent fleet">
  <h2>Agent fleet <small id="fl-sub"></small></h2>
  <div id="fl-banner"></div>
  <div class="fl-totals" id="fl-totals"></div>
  <div class="fl-bar">
    <span class="meta">Show</span>
    <button data-flt="all" class="on">All</button>
    <button data-flt="attn">Needs attention</button>
    <button data-flt="running">Running</button>
    <button data-flt="stopped">Not running</button>
    <span class="sp"></span>
    <span class="meta" id="fl-updated"></span>
  </div>
  <table class="fl" id="fl-table"></table>
  <div class="fl-log" id="fl-log" hidden><h3>Actions</h3><div id="fl-log-rows"></div></div>
  <!-- #138 (blocked on the runner's character-creation endpoint) adds the create form here. -->
  <div class="fl-note">Create agent: not available yet (tracked in #138).</div>
</section>
"""

FLEET_JS = r"""
<script>
const Fleet = (() => {
  const panel = document.getElementById('fleet');
  const btn = document.getElementById('tglFleet');
  // An agent that has written nothing to its audit log for this long is not progressing
  // (the think loop logs every cycle, idle ones included).
  const STALE_S = 120;
  let data = null;        // last answer of /api/fleet (the roster survives a failed poll)
  let failed = null;      // why the last poll failed (wowmap itself unreachable)
  let filter = 'all', timer = null, busy = new Set();
  const logRows = [];

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const badge = (kind, text, title) => { const b = el('span', 'fl-b ' + kind, text); if (title) b.title = title; return b; };
  function age(s) {
    if (s == null || isNaN(s)) return '?';
    s = Math.max(0, Math.round(s));
    if (s < 90) return s + 's';
    if (s < 5400) return Math.round(s / 60) + 'm';
    if (s < 129600) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';
  }
  function since(iso) {
    const t = Date.parse(iso || '');
    return isNaN(t) ? null : age((Date.now() - t) / 1000);
  }
  function playtime(s) {
    if (s == null) return null;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  // The signals of one agent, each derived on its own. Nothing here is combined into a
  // single "healthy": `attn` and `progressing` only feed the filter and the totals.
  // mode: 'runner' (the runner answered), 'probe' (no runner configured: only each agent's own
  // API was probed, so container/audit/brain/character are unknown) or 'unknown' (nothing answered).
  function signals(a, mode) {
    const c = a.container || {}, api = a.agent_api || {}, au = a.audit || {}, br = a.brain || {};
    const unknown = mode === 'unknown', noRunner = mode !== 'runner';
    const cs = noRunner ? 'unknown' : (c.state || 'unknown');
    const running = cs === 'running';
    const apiOk = !unknown && api.state === 'ok';
    // The DB `online` flag is not trusted for agents (#130): only the agent's own API says.
    const loggedIn = (!unknown && apiOk && typeof api.connected === 'boolean') ? api.connected : null;
    const fresh = au.age_seconds != null && au.age_seconds <= STALE_S;
    const progressing = !noRunner && running && apiOk && loggedIn !== false && fresh && br.valid !== false;
    const crashed = cs === 'exited' && c.exit_code != null && c.exit_code !== 0;
    const attn = unknown || (mode === 'probe' ? !apiOk : cs === 'unknown' || crashed || (running && !progressing));
    return {c, api, au, br, cs, running, apiOk, loggedIn, fresh, progressing, crashed, attn, unknown, noRunner};
  }

  function cellContainer(s) {
    const td = el('td');
    const {c, cs} = s;
    if (cs === 'running') {
      td.append(badge('ok', 'running', c.status));
      const t = since(c.since); if (t) td.append(el('div', 'sub', 'up ' + t));
    } else if (cs === 'exited') {
      td.append(badge(s.crashed ? 'bad' : 'off', 'exited' + (c.exit_code != null ? ' ' + c.exit_code : '')));
      const t = since(c.since); if (t) td.append(el('div', 'sub', t + ' ago'));
    } else if (cs === 'absent') {
      td.append(badge('off', 'absent'));
    } else {
      td.append(badge('unk', 'UNKNOWN'));
      if (c.error) td.append(el('div', 'fl-err', c.error));
    }
    return td;
  }
  function cellWorld(s) {
    const td = el('td');
    if (s.unknown) td.append(badge('unk', 'UNKNOWN'));
    else if (s.loggedIn === true) td.append(badge('ok', 'in world'));
    else if (s.loggedIn === false) td.append(badge(s.running || s.noRunner ? 'bad' : 'off', 'not logged in'));
    else {
      // The agent API is down or silent about login: unknown, never "no" and never "yes".
      const stopped = s.cs === 'exited' || s.cs === 'absent';
      td.append(badge(stopped ? 'off' : 'unk', stopped ? 'n/a' : 'UNKNOWN', 'The agent API says nothing about login'));
      if (!stopped && s.au.age_seconds != null) td.append(el('div', 'sub', 'last audit ' + age(s.au.age_seconds) + ' ago'));
    }
    return td;
  }
  function cellApi(s) {
    const td = el('td');
    if (s.unknown) td.append(badge('unk', 'UNKNOWN'));
    else if (s.apiOk) td.append(badge('ok', 'ok'));
    else {
      const stopped = s.cs === 'exited' || s.cs === 'absent';
      td.append(badge(stopped ? 'off' : 'bad', 'unreachable', s.api.error));
      if (s.running) td.append(el('div', 'sub', 'stale image or crashed process?'));
    }
    return td;
  }
  function cellCycle(s) {
    const td = el('td');
    if (s.noRunner) { td.append(badge('unk', 'UNKNOWN')); return td; }
    if (s.au.age_seconds == null) { td.append(badge(s.running ? 'warn' : 'off', 'no audit yet')); return td; }
    td.append(badge(s.running ? (s.fresh ? 'ok' : 'bad') : 'off', age(s.au.age_seconds) + ' ago'));
    if (s.br.cycle != null) td.append(el('div', 'sub', 'cycle ' + s.br.cycle));
    if (s.br.last_error) td.append(el('div', 'fl-err', 'no action taken: ' + s.br.last_error));
    return td;
  }
  function cellBrain(s) {
    const td = el('td');
    if (s.noRunner) { td.append(badge('unk', 'UNKNOWN')); return td; }
    if (!s.au.last_ts) { td.append(badge('off', 'n/a')); return td; }
    const b = s.br;
    td.append(badge(b.valid === false ? 'bad' : b.valid === true ? 'ok' : 'unk',
                    b.valid === false ? 'invalid' : b.valid === true ? 'valid' : 'unknown'));
    const bits = [b.brain, b.model].filter(Boolean);
    if (bits.length) td.append(el('div', 'sub', bits.join(' · ')));
    if (b.last_error && b.valid === false) td.append(el('div', 'fl-err', b.last_error));
    return td;
  }
  function cellChar(a, s) {
    const td = el('td');
    const ch = a.character || {};
    if (s.noRunner) { td.append(badge('unk', 'UNKNOWN')); return td; }
    if (ch.error) { td.append(badge('unk', 'UNKNOWN', ch.source), el('div', 'fl-err', ch.error)); return td; }
    if (ch.level == null && !ch.zone) { td.append(badge('unk', 'UNKNOWN')); return td; }
    td.append(el('span', null, ch.level != null ? 'L' + ch.level : '?'));
    const sub = [ch.zone, playtime(ch.playtime_seconds)].filter(Boolean).join(' · ');
    if (sub) td.append(el('div', 'sub', sub));
    if (ch.source) td.title = 'source: ' + ch.source;
    return td;
  }
  function cellActions(a, s, controls) {
    const td = el('td', 'fl-act');
    if (!controls) {
      td.append(s.noRunner && !s.unknown ? badge('off', 'read-only', 'No agent runner is configured')
                                         : badge('unk', 'unavailable', 'The agent runner cannot be reached'));
      return td;
    }
    const working = busy.has(a.name);
    const b = el('button', null, working ? 'working…' : (s.running ? 'Stop' : 'Start'));
    b.disabled = working;
    b.onclick = () => act(a.name, s.running ? 'stop' : 'start');
    td.append(b);
    return td;
  }

  function counts(rows) {
    const t = {total: rows.length, running: 0, loggedIn: 0, progressing: 0, attn: 0, unknown: 0};
    for (const r of rows) {
      if (r.s.running) t.running++;
      if (r.s.loggedIn === true) t.loggedIn++;
      if (r.s.progressing) t.progressing++;
      if (r.s.attn) t.attn++;
      if (r.s.unknown || r.s.cs === 'unknown') t.unknown++;
    }
    return t;
  }
  function matches(r) {
    return filter === 'all' || (filter === 'attn' && r.s.attn) || (filter === 'running' && r.s.running)
        || (filter === 'stopped' && !r.s.running);
  }

  function render() {
    const runnerState = data ? data.runner : 'unreachable';
    const runnerOk = !!data && !failed && runnerState === 'ok';
    const mode = failed || !data ? 'unknown' : runnerState === 'ok' ? 'runner' : runnerState === 'disabled' ? 'probe' : 'unknown';
    const rows = ((data && data.agents) || []).map((a) => ({a, s: signals(a, mode)}));
    const banner = document.getElementById('fl-banner');
    banner.replaceChildren();
    if (failed) {
      banner.append(el('div', 'fl-banner bad', 'wowmap did not answer (' + failed + '). Everything below is UNKNOWN, not healthy.'));
    } else if (runnerState === 'disabled') {
      banner.append(el('div', 'fl-banner warn', 'Read-only: no agent runner is configured (AGENT_RUNNER_URL is empty), so container, '
        + 'last-cycle, brain and character state is UNKNOWN and start/stop is unavailable. Only each agent\'s own API is probed.'));
    } else if (!runnerOk) {
      banner.append(el('div', 'fl-banner bad', 'Agent runner ' + runnerState + (data && data.error ? ': ' + data.error : '')
        + '. Fleet state is UNKNOWN, not healthy.'));
    }
    if (runnerOk && data.image && data.image.stale === true) {
      banner.append(el('div', 'fl-banner warn', 'The agent image is older than the last change to agent/ or the Dockerfile; '
        + 'a running agent may lack the observability API (#131). Start with a rebuild to refresh it.'));
    } else if (runnerOk && data.image && data.image.present === false) {
      banner.append(el('div', 'fl-banner warn', 'The agent image has not been built yet.'));
    }

    const t = counts(rows);
    const totals = document.getElementById('fl-totals');
    totals.replaceChildren();
    const stat = (n, label, cls) => { const d = el('div', 'stat' + (cls ? ' ' + cls : '')); d.append(el('b', null, n), el('span', null, label)); return d; };
    // '?' where the number would be a guess: with no runner there is no container state to count.
    const q = (n) => (mode === 'runner' ? n : '?');
    totals.append(stat(mode === 'unknown' && !rows.length ? '?' : rows.length, 'agents'),
                  stat(q(t.running), 'running'),
                  stat(mode === 'unknown' ? '?' : t.loggedIn, 'logged in'),
                  stat(q(t.progressing), 'progressing'),
                  stat(mode === 'unknown' ? '?' : t.attn, 'need attention', mode !== 'unknown' && t.attn ? 'attn' : ''),
                  stat(mode === 'runner' ? t.unknown : '?', 'unknown'));

    // Broken first, then stopped last, so a failure is the first thing on the page.
    const rank = (r) => (r.s.attn ? 0 : r.s.running ? 1 : 2);
    const shown = rows.filter(matches).sort((x, y) => rank(x) - rank(y) || x.a.name.localeCompare(y.a.name));
    document.querySelectorAll('#fleet [data-flt]').forEach((b) => b.classList.toggle('on', b.dataset.flt === filter));

    const table = document.getElementById('fl-table');
    const head = el('tr');
    for (const h of ['Agent', 'Container', 'Login / in world', 'Agent API', 'Last cycle', 'Brain', 'Character', ''])
      head.append(el('th', null, h));
    const thead = el('thead'); thead.append(head);
    const tbody = el('tbody');
    const controls = !!data && data.controls && runnerOk;
    for (const {a, s} of shown) {
      const tr = el('tr', (s.attn ? 'attn ' : '') + (!s.running && !s.attn ? 'stopped' : ''));
      const nm = el('td'); nm.append(el('div', 'nm', a.name));
      if (a.account) nm.append(el('div', 'sub', a.account));
      tr.append(nm, cellContainer(s), cellWorld(s), cellApi(s), cellCycle(s), cellBrain(s), cellChar(a, s),
                cellActions(a, s, controls));
      tbody.append(tr);
    }
    if (!shown.length) {
      const tr = el('tr'), td = el('td', 'sub', rows.length ? 'No agent matches this filter.'
        : (runnerOk ? 'The runner has no agents configured.' : 'No agent to show.'));
      td.colSpan = 8; tr.append(td); tbody.append(tr);
    }
    table.replaceChildren(thead, tbody);
    document.getElementById('fl-sub').textContent = (data && data.generated_at ? 'runner time ' + data.generated_at : '');
    document.getElementById('fl-updated').textContent = 'updated ' + new Date().toLocaleTimeString();
    renderLog();
  }

  function renderLog() {
    const box = document.getElementById('fl-log');
    box.hidden = !logRows.length;
    document.getElementById('fl-log-rows').replaceChildren(...logRows.map((r) => el('div', 'row ' + (r.ok ? 'ok' : 'bad'), r.text)));
  }
  function note(ok, text) {
    logRows.unshift({ok, text: new Date().toLocaleTimeString() + '  ' + text});
    logRows.length = Math.min(logRows.length, 10);
  }

  async function act(name, action) {
    const verb = action === 'stop' ? 'Stop' : 'Start';
    const extra = action === 'start' ? ' This may rebuild the image and take a while.' : ' The character will log out.';
    if (!confirm(`${verb} agent ${name}?${extra}`)) return;
    busy.add(name); render();
    try {
      const r = await fetch(`/api/fleet/agents/${encodeURIComponent(name)}/${action}`,
                            {method: 'POST', headers: {'X-Fleet-Action': '1'}});
      const j = await r.json().catch(() => ({error: 'unreadable answer (HTTP ' + r.status + ')'}));
      if (r.ok) {
        const c = (j.agent && j.agent.container) || {};
        note(true, `${action} ${name}: container ${c.state || '?'}` + (c.exit_code != null ? ` (exit ${c.exit_code})` : '')
                   + (j.built ? ', image rebuilt' : ''));
      } else {
        note(false, `${action} ${name} failed: ${j.error || 'HTTP ' + r.status}` + (j.detail ? '\n' + j.detail : ''));
      }
    } catch (e) {
      note(false, `${action} ${name}: wowmap did not answer (${e.message}); the action may or may not have run`);
    }
    busy.delete(name);
    await poll();
  }

  async function poll() {
    try {
      const r = await fetch('/api/fleet', {cache: 'no-store'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      data = await r.json(); failed = null;
    } catch (e) {
      failed = e.message || 'error';
      // Keep the roster so each row can say UNKNOWN; its old signals must not be shown as current.
      if (data) data = {runner: 'unreachable', error: null, agents: data.agents, controls: false};
    }
    if (!panel.hidden) render();
  }

  function visible() { return !panel.hidden && !document.hidden; }
  function setOpen(open) {
    panel.hidden = !open;
    btn.classList.toggle('on', open);
    try { history.replaceState(null, '', open ? '#fleet' : location.pathname + location.search); } catch (e) { /* ignore */ }
    clearInterval(timer);
    if (open) { render(); poll(); timer = setInterval(() => { if (visible()) poll(); }, 5000); }
  }
  btn.onclick = () => setOpen(panel.hidden);
  document.querySelectorAll('#fleet [data-flt]').forEach((b) => { b.onclick = () => { filter = b.dataset.flt; render(); }; });
  document.addEventListener('visibilitychange', () => { if (visible()) poll(); });
  if (location.hash === '#fleet') setOpen(true);
  return {setOpen, poll};
})();
</script>
"""
