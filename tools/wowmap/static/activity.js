// UM-76: "Recent activity" section of the inspect drawer, fed by
// GET /api/character/<name>/activity (see activity.py). Inspect.renderBody appends
// this module's persistent node; the module polls on its own every 5 s while the
// browser tab is visible. #173: each entry shows a clock time and a relative age,
// the list scrolls inside the drawer under its own search box (client-side filter
// over the fetched window, match highlighted), and the footer says how far back the
// window goes. Event text comes from chat and the database, so it is rendered with
// textContent only (the highlight is built from text nodes and <mark>).
window.ActivityFeed = (() => {
  const ICONS = {combat: '⚔', loot: '✚', item: '✚', item_lost: '✖', sell: '$', buy: '$',
    money: '¤', trade: '⇄', chat: '“', whisper: '“', level: '▲', zone: '➜', quest: '!',
    mail: '✉', move: '→', group: '◆', session: '●', action: '•'};
  const SOURCES = {db: 'database', chat: 'public chat', audit: 'agent log'};
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const box = el('div', 'activity');
  const search = el('input', 'act-search');
  search.type = 'search';
  search.placeholder = 'Search activity (kind, text, zone, who)';
  search.setAttribute('aria-label', 'Search recent activity');
  const list = el('div', 'act-list');
  box.append(el('h3', null, 'Recent activity'), search, list);
  let shownFor = null, seq = 0, data = null;
  const LIMIT = 50;

  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
  // What the search box matches: only what a row shows (text names the partner,
  // channel and zone), so every match can be highlighted.
  const hay = (e) => [e.text, e.kind, SOURCES[e.source] || e.source].join(' ').toLowerCase();
  // Append `text` to `parent` with every occurrence of `q` wrapped in <mark>.
  function hl(parent, text, q) {
    const s = String(text ?? '');
    if (!q) { parent.append(s); return; }
    let i = 0, j;
    while ((j = s.toLowerCase().indexOf(q, i)) !== -1) {
      parent.append(s.slice(i, j), el('mark', null, s.slice(j, j + q.length)));
      i = j + q.length;
    }
    parent.append(s.slice(i));
  }

  function render() {
    const f = document.createDocumentFragment();
    const src = (data && data.sources) || {};
    if (!data) f.append(el('div', 'none', 'loading…'));
    else if (data.error) f.append(el('div', 'none', data.error));
    else {
      for (const [key, label] of [['db', 'database'], ['chat', 'chat feed']]) {
        if (src[key] && !src[key].ok) f.append(el('div', 'note', `${label} unreachable, events may be missing`));
      }
      const q = search.value.trim().toLowerCase();
      const shown = q ? data.events.filter((e) => hay(e).includes(q)) : data.events;
      if (!data.events.length) f.append(el('div', 'none', 'no activity recorded yet'));
      else if (!shown.length) f.append(el('div', 'none', `no matches for "${search.value.trim()}"`));
      for (const e of shown) {
        const failed = e.detail && e.detail.ok === false;
        const r = el('div', 'act' + (failed ? ' failed' : ''));
        r.append(el('span', 'ic k-' + e.kind, ICONS[e.kind] || '•'));
        const tx = el('div', 'tx');
        if (e.kind === 'money' && e.detail && Number.isFinite(e.detail.delta)) {
          // Same renderer as the drawer and the tooltip; the label still highlights.
          hl(tx, 'Money ' + (e.detail.delta > 0 ? '+' : '−'), q);
          tx.append(Coins.render(Math.abs(e.detail.delta)));
        } else hl(tx, e.text, q);
        if (e.inferred) {
          const b = el('span', 'inferred', 'inferred');
          b.title = 'Not recorded by the server: deduced from database changes';
          tx.append(b);
        }
        const meta = el('div', 'meta', `${clock(e.t)} · ${ago(e.t)} · `);
        hl(meta, e.kind, q);
        meta.append(' · ');
        hl(meta, SOURCES[e.source] || e.source, q);
        meta.title = new Date(e.t * 1000).toLocaleString();
        tx.append(meta);
        r.append(tx);
        f.append(r);
      }
      // The window is bounded, so say where it ends instead of looking endless.
      const ev = data.events, last = ev[ev.length - 1];
      if (last) f.append(el('div', 'note', (q ? `${shown.length} match${shown.length === 1 ? '' : 'es'} in ` : '') +
        (ev.length < LIMIT ? `all ${ev.length} recorded events` : `the newest ${ev.length} events`) +
        `, back to ${new Date(last.t * 1000).toLocaleString()} (${ago(last.t)})`));
    }
    const top = list.scrollTop;
    list.replaceChildren(f);
    list.scrollTop = top;
  }
  search.oninput = render;

  async function refresh() {
    const who = Inspect.current();
    if (!who || who !== shownFor) return;
    const mine = ++seq;
    try {
      const r = await fetch(`/api/character/${encodeURIComponent(who)}/activity?limit=${LIMIT}`);
      const j = await r.json().catch(() => ({}));
      if (mine !== seq || who !== shownFor) return;
      data = r.ok ? j : {error: j.error || 'error ' + r.status};
    } catch (e) {
      if (mine !== seq) return;
      data = {error: 'activity unavailable: ' + e};
    }
    render();
  }

  // Called by Inspect.renderBody on every re-render; the node is reused. Re-attaching
  // it blurs the search box and resets the list's scroll, so put both back once the
  // drawer body has been replaced.
  function section(name) {
    if (name !== shownFor) {
      shownFor = name;
      data = null;
      seq++;
      search.value = '';
      render();
      refresh();
    }
    const typing = document.activeElement === search, top = list.scrollTop;
    const [a, b] = [search.selectionStart, search.selectionEnd];
    queueMicrotask(() => {
      list.scrollTop = top;
      if (typing) { search.focus({preventScroll: true}); search.setSelectionRange(a, b); }
    });
    return box;
  }

  // Each poll also re-renders, which keeps the relative ages current. No timer work
  // while the browser tab is hidden; catch up as soon as it is shown again.
  setInterval(() => { if (!document.hidden) refresh(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  return {section, refresh};
})();
