// UM-50: agents that run an observability API (AGENT_API_URLS) get a distinct
// ring on the map and an "Agent mind" tab in the inspect drawer, polled through
// /api/agent/<name>/{brain,perception}. Self-contained: it only reads
// Inspect.current() and adds its own elements, so the inspect panel is untouched.
// Everything from the agent is rendered with textContent (chat and names are untrusted).
const AgentMind = (() => {
  const drawer = document.getElementById('inspect');
  const charBody = document.getElementById('inspect-body');
  const status = document.getElementById('inspect-status');
  const agents = new Map();  // lowercased name -> name
  let tab = 'character', shownFor = null, seq = 0;
  // #174: /api/character/<n>/kind for shownFor (agent or human, decided server-side),
  // and the last time each agent's brain answered.
  let kind = null;
  const lastOk = new Map();

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function kv(label, value) {
    const r = el('div', 'kv');
    r.append(el('span', 'k', label), el('span', 'v', value));
    return r;
  }
  const isAgent = (n) => !!n && agents.has(n.toLowerCase());

  const tabs = el('div', 'mind-tabs');
  const bChar = el('button', 'on', 'Character'), bAct = el('button', null, 'Activity'),
        bMind = el('button', null, 'Agent mind');
  tabs.append(bChar, bAct, bMind);
  tabs.hidden = true;
  const pane = el('div', 'drawer-body mind');
  pane.hidden = true;
  // #210: recent activity has its own tab; the list scrolls inside a fixed-height pane.
  const actPane = el('div', 'drawer-body act-pane');
  actPane.hidden = true;
  drawer.insertBefore(tabs, charBody);
  drawer.insertBefore(actPane, status);
  drawer.insertBefore(pane, status);

  // ActivityFeed.section() restarts the feed when the character changes; the node is reused.
  function mountActivity() {
    const n = Inspect.current();
    if (n && window.ActivityFeed) actPane.replaceChildren(window.ActivityFeed.section(n));
  }

  function setTab(t) {
    tab = t;
    bChar.classList.toggle('on', t === 'character');
    bAct.classList.toggle('on', t === 'activity');
    bMind.classList.toggle('on', t === 'mind');
    charBody.hidden = t !== 'character';
    actPane.hidden = t !== 'activity';
    pane.hidden = t !== 'mind';
    if (t === 'activity') mountActivity();
    if (t === 'mind') refresh();
  }
  bChar.onclick = () => setTab('character');
  bAct.onclick = () => setTab('activity');
  bMind.onclick = () => setTab('mind');

  function sync() {
    const n = Inspect.current();
    tabs.hidden = !n;
    if (n !== shownFor) {
      shownFor = n;
      kind = null;
      seq++;
      pane.replaceChildren(el('div', 'none', 'loading…'));
      if (n) loadKind(n);
      if (tab === 'activity') mountActivity();
    }
  }

  async function loadKind(n) {
    try {
      const r = await fetch(`/api/character/${encodeURIComponent(n)}/kind`);
      const j = await r.json().catch(() => ({}));
      if (n !== shownFor) return;
      kind = r.ok ? j : {kind: 'error', error: r.status === 404 ? 'character not found' : j.error || 'error ' + r.status};
    } catch (e) {
      if (n !== shownFor) return;
      kind = {kind: 'error', error: e.message};
    }
    refresh();
  }

  function say(...lines) {
    pane.replaceChildren(...lines.map((t, i) => el('div', i ? 'none' : null, t)));
  }

  function args(a) {
    const s = JSON.stringify(a || {});
    return s === '{}' ? '' : s.length > 120 ? s.slice(0, 117) + '...' : s;
  }
  function unitLine(u) {
    const bits = [u.name || `${u.type || 'object'} ${u.entry ?? ''}`];
    if (u.level != null) bits.push(`L${u.level}`);
    if (u.health_pct != null) bits.push(`${Math.round(u.health_pct * 100)}% hp`);
    if (u.in_combat) bits.push('in combat');
    return bits.join(' · ');
  }

  function render(brain, perc) {
    const f = document.createDocumentFragment();
    f.append(el('h3', null, 'Brain'));
    f.append(kv('Status', brain.connected ? 'in game' : 'not connected'));
    f.append(kv('Goal', brain.goal || '(none)'));
    if (brain.brain) f.append(kv('Brain', brain.brain));
    f.append(kv('Model', brain.model || '(none)'));
    f.append(kv('Cycle', brain.cycle ?? '-'));
    const t = brain.tokens || {};
    f.append(kv('Tokens', `${t.prompt_total || 0} in · ${t.completion_total || 0} out · ${t.cycles || 0} cycles`));
    for (const [k, v] of Object.entries(brain.reflexes || {})) f.append(kv(`Reflex: ${k}`, JSON.stringify(v)));

    f.append(el('h3', null, 'Last decisions'));
    const decs = (brain.decisions || []).slice().reverse();
    if (!decs.length) f.append(el('div', 'none', 'no decisions yet'));
    for (const d of decs) {
      const ok = d.result && d.result.ok;
      const r = el('div', 'dec' + (ok ? '' : ' fail'));
      const tc = d.tool_call || {};
      // UM-101: which brain picked it, Jev's confidence, and a fallback marker.
      const who = d.brain ? ` · ${d.brain}${d.confidence != null ? ' ' + Math.round(d.confidence * 100) + '%' : ''}${d.fallback && d.brain === 'llm' ? ' (jev fallback)' : ''}` : '';
      r.append(el('div', 'when', `#${d.cycle} · ${d.ts ? new Date(d.ts * 1000).toLocaleTimeString() : ''}${who}`),
               el('div', 'act', `${tc.name || '(no action)'} ${args(tc.args)}`));
      if (!ok && d.result && d.result.error) r.append(el('div', 'err', d.result.error));
      f.append(r);
    }

    f.append(el('h3', null, 'Nearby'));
    if (perc && perc.connected !== false) {
      const p = perc.position;
      if (p) f.append(kv('Position', `map ${p.map} · ${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)}`));
      for (const [label, key] of [['Units', 'nearby_units'], ['Players', 'nearby_players'], ['Objects', 'nearby_objects']]) {
        const list = (perc[key] || []).slice(0, 8);
        f.append(el('h4', null, `${label} (${(perc[key] || []).length})`));
        if (!list.length) f.append(el('div', 'none', 'none'));
        for (const u of list) f.append(kv(`${u.distance} yd`, unitLine(u)));
      }
      for (const key of ['window', 'trade', 'pending_invite']) {
        if (perc[key]) f.append(kv(key.replace('_', ' '), JSON.stringify(perc[key]).slice(0, 200)));
      }
    } else {
      f.append(el('div', 'none', 'no perception'));
    }
    const top = pane.scrollTop;
    pane.replaceChildren(f);
    pane.scrollTop = top;
  }

  async function getJson(who, view) {
    const r = await fetch(`/api/agent/${encodeURIComponent(who)}/${view}` + (view === 'brain' ? '?n=5' : ''));
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.error || 'error ' + r.status), {code: j.code});
    return j;
  }

  async function refresh() {
    const who = Inspect.current();
    if (!who || who !== shownFor || tab !== 'mind' || !kind) return;
    if (kind.kind === 'error') return say(`Could not tell whether ${who} is an agent`, kind.error);
    if (kind.kind === 'human') return say('Human player — no agent brain attached', `Character: ${kind.name}`);
    if (!kind.fleet_configured) {
      return say(`Agent ${kind.name} (${kind.account}) — the agent fleet is not configured on this page`,
                 'AGENT_API_URLS is empty, so wowmap has no brain API to ask.');
    }
    if (!kind.agent_api) {
      return say(`Agent ${kind.name} (${kind.account}) — no brain API for this agent is configured on this page`);
    }
    const mine = ++seq;
    try {
      const [brain, perc] = await Promise.all([getJson(who, 'brain'), getJson(who, 'perception')]);
      if (mine !== seq) return;
      lastOk.set(who, new Date());
      render(brain, perc);
      status.textContent = 'agent updated ' + new Date().toLocaleTimeString();
    } catch (e) {
      if (mine !== seq) return;
      // #264: the agent answered, in a version this page cannot render (not "unreachable").
      if (e.code === 'api_version') return say(`Agent ${kind.name} — ${e.message}`, 'Update wowmap or the agent so their API versions match.');
      const last = lastOk.get(who);
      say(`Agent ${kind.name} — brain API unreachable`,
          'last successful poll: ' + (last ? last.toLocaleTimeString() : 'never this session'), e.message);
    }
  }

  function tag(root) {
    for (const e of root.querySelectorAll('.marker, .pl')) e.classList.toggle('agent', isAgent(e.dataset.name));
  }
  async function loadAgents() {
    try {
      const j = await (await fetch('/api/agents')).json();
      agents.clear();
      for (const n of j.agents || []) agents.set(n.toLowerCase(), n);
      tag(document);
      sync();
    } catch (e) {
      status.textContent = 'agent list unavailable: ' + e.message;
    }
  }
  // The player list is rebuilt on every tick; tag the new nodes. Map markers live in
  // Leaflet's marker pane, so the page calls AgentMind.tag() when it places them.
  const list = document.getElementById('list');
  if (list) new MutationObserver(() => tag(list)).observe(list, {childList: true});

  loadAgents();
  setInterval(loadAgents, 60000);
  setInterval(sync, 500);
  setInterval(refresh, 3000);
  return {refresh, tag, agents: () => [...agents.values()]};
})();
