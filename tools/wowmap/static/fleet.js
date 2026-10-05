// Every string from the runner (character names, gateway error text, docker
// stderr) is untrusted and rendered with textContent only, never as markup.
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
