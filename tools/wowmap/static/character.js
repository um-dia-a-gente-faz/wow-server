// Everything from the API is rendered with textContent (never innerHTML): character
// and item names come straight from the database.
const Inspect = (() => {
  const EQUIP_SLOTS = ['Head', 'Neck', 'Shoulder', 'Shirt', 'Chest', 'Waist', 'Legs',
    'Feet', 'Wrist', 'Hands', 'Finger 1', 'Finger 2', 'Trinket 1', 'Trinket 2', 'Back',
    'Main Hand', 'Off Hand', 'Ranged', 'Tabard'];
  const POWER_LABELS = {mana: 'Mana', rage: 'Rage', focus: 'Focus', energy: 'Energy',
    happiness: 'Happiness', rune: 'Runes', runic_power: 'Runic Power'};
  // The server keeps rage and runic power in tenths (1000 is shown as 100 in game).
  const POWER_SCALE = {rage: 10, runic_power: 10};
  // Bar colours roughly follow the default unit frames.
  const BAR_COLORS = {health: '#1f9e3a', mana: '#2f5fd8', rage: '#c42f2f', focus: '#d98a3a',
    energy: '#d6c22e', happiness: '#2fa88a', rune: '#7f7f7f', runic_power: '#1fa6c4'};
  const CLASS_POWERS = {1: ['rage'], 2: ['mana'], 3: ['mana'], 4: ['energy'], 5: ['mana'],
    6: ['runic_power'], 7: ['mana'], 8: ['mana'], 9: ['mana'], 11: ['mana', 'rage', 'energy']};
  const nf = new Intl.NumberFormat('en-US');
  const drawer = document.getElementById('inspect');
  const head = document.getElementById('inspect-head');
  const body = document.getElementById('inspect-body');
  const status = document.getElementById('inspect-status');
  const openSections = new Set();
  let name = null, lastJson = null, seq = 0;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function kv(label, value, cls = 'kv') {
    const r = el('div', cls);
    const v = el('span', 'v');
    if (value != null) v.append(value);
    r.append(el('span', 'k', label), v);
    return r;
  }
  // A bar when the max is known (character_stats row), otherwise the plain number.
  function meter(label, cur, max, color) {
    const value = nf.format(cur);
    if (!(max > 0)) return kv(label, value);
    const b = el('div', 'meter-bar');
    const fill = el('span', 'fill');
    fill.style.width = `${Math.max(0, Math.min(100, cur / max * 100))}%`;
    fill.style.background = color;
    b.append(fill, el('span', 'txt', `${value} / ${nf.format(max)}`));
    const r = el('div', 'kv');
    r.append(el('span', 'k', label));
    const v = el('span', 'v');
    v.append(b);
    r.append(v);
    return r;
  }
  // #176: icon in front of a kv/meter row's label, exact value on hover.
  function stat(name, label, row, title) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', `si ${name}`);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
    const t = document.createElementNS(svg.namespaceURI, 'title');
    t.textContent = label;
    const use = document.createElementNS(svg.namespaceURI, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(t, use);
    row.querySelector('.k').prepend(svg);
    if (title) row.title = title;
    return row;
  }
  function duration(sec) {
    const s = Number(sec) || 0;
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString('en-US') : 'never'; }

  // bag 0 = the character's own slots (TrinityCore Player.h EquipmentSlots, InventorySlots, …);
  // any other bag value is the item_instance guid of the container holding the item.
  function groupInventory(items) {
    const g = {equipped: [], backpack: [], bags: [], bank: [], bankBags: [], keyring: [],
               currency: [], other: []};
    const containers = new Map();
    for (const it of items) {
      if (it.bag !== 0) continue;
      const s = it.slot;
      if (s < 19) g.equipped.push(it);
      else if (s < 23 || (s >= 67 && s < 74)) {
        const bank = s >= 67;
        const c = {slot: s, bag: it, items: []};
        (bank ? g.bankBags : g.bags).push(c);
        containers.set(it.item_guid, c);
      }
      else if (s < 39) g.backpack.push(it);
      else if (s < 67) g.bank.push(it);
      else if (s >= 86 && s < 118) g.keyring.push(it);
      else if (s >= 118 && s < 150) g.currency.push(it);
      else g.other.push(it);
    }
    for (const it of items) {
      if (it.bag === 0) continue;
      const c = containers.get(it.bag);
      (c ? c.items : g.other).push(it);
    }
    return g;
  }

  // item_template.Quality 0..7: poor, common, uncommon, rare, epic, legendary, artifact, heirloom.
  const Q_COLORS = ['#9d9d9d', '#ffffff', '#1eff00', '#0070dd', '#a335ee', '#ff8000', '#e6cc80', '#e6cc80'];
  // `it.icon` is a same-origin /icons/<file>.png URL from the API, or null (no icon
  // extracted): then a '?' placeholder is drawn, as it is if the image fails to load.
  function itemIcon(it) {
    const q = Q_COLORS[it.quality];
    const box = el('span', 'ico');
    box.setAttribute('aria-label', it.count > 1 ? `${it.item_name} ×${it.count}` : it.item_name);
    box.tabIndex = 0;
    ItemTip.attach(box, it);
    if (q) box.style.borderColor = q;
    if (it.icon) {
      const img = el('img');
      img.alt = '';
      img.loading = 'lazy';
      img.addEventListener('error', () => { img.remove(); box.classList.add('ph'); });
      img.src = it.icon;
      box.append(img);
    } else box.classList.add('ph');
    if (it.count > 1) box.append(el('span', 'cnt', it.count));
    return box;
  }

  function itemRows(parent, items, label) {
    if (!items.length) { parent.append(el('div', 'none', 'empty')); return; }
    for (const it of items) {
      const r = el('div', 'item');
      r.append(el('span', 'k', label(it)), itemIcon(it));
      const v = el('span', 'v', it.item_name);
      if (Q_COLORS[it.quality]) v.style.color = Q_COLORS[it.quality];
      v.append(' ', el('span', 'n', '×' + it.count));
      r.append(v);
      parent.append(r);
    }
  }

  function collapsible(key, title) {
    const d = el('details');
    d.dataset.key = key;
    d.open = openSections.has(key);
    d.append(el('summary', null, title));
    d.addEventListener('toggle', () => {
      d.open ? openSections.add(key) : openSections.delete(key);
    });
    return d;
  }

  // ---- In-game style equipment and bags (UM-80) ----------------------------------
  // Paper doll: the character window's slot columns (EquipmentSlots ids, Player.h).
  const DOLL_LEFT = [0, 1, 2, 14, 4, 3, 18, 8];      // head neck shoulder back chest shirt tabard wrist
  const DOLL_RIGHT = [9, 5, 6, 7, 10, 11, 12, 13];   // hands waist legs feet finger×2 trinket×2
  const DOLL_BOTTOM = [15, 16, 17];                  // main hand, off hand, ranged
  // An empty slot: a dimmed square named after the slot, like the game's slot art.
  function emptySlot(label) {
    const e = el('span', 'ico empty');
    if (label) { e.title = label; e.append(el('span', 'lbl', label)); }
    return e;
  }
  // #171: the 3D body. Without an extracted model (or WebGL) the box says so; nothing else moves.
  function characterModel(c) {
    const box = el('div', 'model');
    const none = (why) => box.replaceChildren(el('div', 'none', why));
    if (!c.model) { none('No 3D model extracted'); return box; }
    if (!window.CharView) { none('3D viewer unavailable'); return box; }
    box.title = 'Drag to rotate, scroll to zoom';
    window.CharView.mount(box, c.model).catch(() => none('3D model unavailable'));
    return box;
  }
  function paperDoll(equipped, c) {
    const by = new Map(equipped.map((it) => [it.slot, it]));
    const sq = (s) => by.has(s) ? itemIcon(by.get(s)) : emptySlot(EQUIP_SLOTS[s]);
    const doll = el('div', 'doll');
    const left = el('div', 'col'), right = el('div', 'col'), bottom = el('div', 'bottom');
    left.append(...DOLL_LEFT.map(sq));
    right.append(...DOLL_RIGHT.map(sq));
    bottom.append(...DOLL_BOTTOM.map(sq));
    const mid = el('div', 'mid');
    mid.append(el('div', 'nm', c.name), el('div', null, `Level ${c.level} ${c.race_name}`),
               el('div', null, c.class_name));
    mid.firstChild.style.color = c.class_color;
    mid.append(characterModel(c));
    doll.append(left, mid, right, bottom);
    return doll;
  }
  // One bag window: `size` squares, `cols` wide. Like the game's container frames
  // the slots fill from the bottom right, so a partial row sits top right.
  function bagGrid(title, quality, size, items, first, cols) {
    const box = el('div', 'bag');
    const h = el('div', 'bag-title', title);
    if (Q_COLORS[quality]) h.style.color = Q_COLORS[quality];
    const grid = el('div', 'bag-grid');
    grid.style.gridTemplateColumns = `repeat(${cols}, 36px)`;
    const by = new Map(items.map((it) => [it.slot - first, it]));
    const n = Math.max(size, ...[...by.keys()].map((k) => k + 1), 0);
    for (let i = 0; i < (cols - n % cols) % cols; i++) grid.append(el('span', 'gap'));
    for (let i = 0; i < n; i++) grid.append(by.has(i) ? itemIcon(by.get(i)) : emptySlot());
    box.append(h, grid);
    return box;
  }
  // The bag bar (the four bag slots, InventorySlots 19-22, or bank bags 67-73)
  // followed by a grid per equipped bag.
  function bagWindows(parent, packTitle, pack, packSize, packFirst, packCols, bags, firstBagSlot, nBags) {
    const bySlot = new Map(bags.map((b) => [b.slot, b]));
    const bar = el('div', 'bag-bar');
    for (let s = firstBagSlot; s < firstBagSlot + nBags; s++) {
      bar.append(bySlot.has(s) ? itemIcon(bySlot.get(s).bag) : emptySlot('Bag'));
    }
    const wrap = el('div', 'bags');
    wrap.append(bagGrid(packTitle, 1, packSize, pack, packFirst, packCols));
    for (const b of [...bags].sort((x, y) => x.slot - y.slot)) {
      wrap.append(bagGrid(b.bag.item_name, b.bag.quality, b.bag.container_slots, b.items, 0, 4));
    }
    parent.append(bar, wrap);
  }

  function renderHead(c) {
    const h = el('h2');
    const dot = el('span', 'dot');
    dot.style.background = dot.style.color = c.class_color;
    h.append(dot, el('span', 'nm', c.name),
             el('span', 'badge' + (c.online ? ' on' : ''), c.online ? 'online' : 'offline'));
    head.replaceChildren(h,
      el('div', 'line', `Level ${c.level} · ${c.race_name} · ${c.class_name}`),
      el('div', 'line', placeText(c)));
  }

  // Names come from the client DBCs (tools/dbc/names.py); unknown ids fall back to the raw id.
  function talentSection(list) {
    const d = collapsible('talents', `Talents (${list.length})`);
    const groups = new Map();
    for (const t of list) {
      const key = `${t.spec}|${t.tree_order ?? 99}|${t.tree ?? ''}`;
      if (!groups.has(key)) groups.set(key, {spec: t.spec, order: t.tree_order ?? 99, tree: t.tree, items: []});
      groups.get(key).items.push(t);
    }
    const specs = new Set(list.map((t) => t.spec));
    const sorted = [...groups.values()].sort((a, b) => a.spec - b.spec || a.order - b.order);
    for (const g of sorted) {
      const tree = g.tree || 'Unknown tree';
      d.append(el('h4', null, specs.size > 1 ? `Spec ${g.spec + 1} · ${tree} (${g.items.length})` : `${tree} (${g.items.length})`));
      for (const t of g.items) d.append(kv(t.name || `spell ${t.spell}`, t.rank ? `rank ${t.rank}` : ''));
    }
    return d;
  }
  // The in-game reputation window (GameNames.reputation_panel): headers from the
  // Faction.dbc tree, only factions the character has discovered. Bar colours are
  // FACTION_BAR_COLORS from the 3.3.5 FrameXML ReputationFrame.lua, keyed by rank_id.
  const REP_COLORS = {1: '#cc4d38', 2: '#cc4d38', 3: '#bf4500', 4: '#e6b300',
    5: '#00991a', 6: '#00991a', 7: '#00991a', 8: '#00991a'};
  const closedReps = new Set();
  function repRow(name, r) {
    const row = el('div', 'rep');
    row.append(el('span', 'rep-name', name));
    const b = el('span', 'rep-bar' + (r.at_war ? ' war' : ''));
    const fill = el('span', 'fill');
    fill.style.width = `${Math.max(0, Math.min(100, r.bar_value / r.bar_max * 100))}%`;
    fill.style.background = REP_COLORS[r.rank_id] || '#888';
    b.append(fill, el('span', 'txt', r.rank));
    if (r.at_war) b.title = 'At war';
    row.append(b, el('span', 'rep-num', `${r.bar_value}/${r.bar_max}`));  // as in game: 562/3000
    return row;
  }
  function repNodes(parent, nodes) {
    for (const n of nodes) {
      if (!n.header) { parent.append(repRow(n.name, n.rep)); continue; }
      const key = String(n.faction ?? n.name);
      const d = el('details', 'rep-group');
      d.open = !closedReps.has(key);
      const s = el('summary');
      s.append(n.rep ? repRow(n.name, n.rep) : el('span', 'rep-head', n.name));
      d.append(s);
      d.addEventListener('toggle', () => { d.open ? closedReps.delete(key) : closedReps.add(key); });
      repNodes(d, n.children || []);
      parent.append(d);
    }
  }
  function countReps(nodes) {
    return nodes.reduce((k, n) => k + (n.rep ? 1 : 0) + countReps(n.children || []), 0);
  }
  function reputationSection(panel) {
    const d = collapsible('reputation', `Reputation (${countReps(panel)})`);
    if (!panel.length) d.append(el('div', 'none', 'No factions discovered'));
    repNodes(d, panel);
    return d;
  }
  function achievementSection(list) {
    const points = list.reduce((n, a) => n + (a.points || 0), 0);
    const d = collapsible('achievements', `Achievements (${list.length} · ${nf.format(points)} pts)`);
    for (const a of list) {
      const pts = a.points != null ? `${a.points} pts · ` : '';
      d.append(kv(a.name || `#${a.achievement}`, pts + when(a.date)));
    }
    return d;
  }

  function renderBody(c) {
    const f = document.createDocumentFragment();

    f.append(el('h3', null, 'Status'));
    f.append(stat('health', 'Health', meter('Health', c.health, c.max_health, BAR_COLORS.health),
                  `health ${c.health} / ${c.max_health ?? '?'}`));
    const power = c.power || {}, maxPower = c.max_power || {};
    for (const key of CLASS_POWERS[c.class] || Object.keys(POWER_LABELS)) {
      if (!(key in power)) continue;
      const scale = POWER_SCALE[key] || 1;
      const row = meter(POWER_LABELS[key], Math.floor(power[key] / scale),
                        Math.floor((maxPower[key] || 0) / scale), BAR_COLORS[key]);
      f.append(key === 'mana' ? stat('mana', 'Mana', row, `mana ${power.mana} / ${maxPower.mana ?? '?'}`) : row);
    }
    f.append(stat('gold', 'Gold', kv('Gold', Coins.render(c.money)), `${nf.format(c.money)} copper`));
    f.append(stat('played', 'Played time', kv('Played time', c.totaltime ? duration(c.totaltime) : '—'),
                  `played ${c.totaltime}s`));
    f.append(stat('logout', 'Last logout', kv('Last logout', c.logout_time ? ago(c.logout_time) : '—'),
                  `last logout ${when(c.logout_time)}`));

    f.append(el('h3', null, 'Position'));
    f.append(kv('Location', placeText(c)));
    f.append(kv('Map coords', mapCoordsText(c) || '—'));
    f.append(kv('World X, Y, Z', worldText(c.position_x, c.position_y, c.position_z)));
    f.append(kv('Facing', `${c.orientation.toFixed(2)} rad`));

    const inv = groupInventory(c.inventory || []);
    f.append(el('h3', null, 'Equipped'));
    f.append(paperDoll(inv.equipped, c));
    f.append(el('h3', null, 'Bags'));
    // Backpack: InventoryPackSlots 23-38; bags in InventorySlots 19-22 (Player.h).
    bagWindows(f, 'Backpack', inv.backpack, 16, 23, 4, inv.bags, 19, 4);

    if (inv.bank.length || inv.bankBags.length) {
      const d = collapsible('bank', `Bank (${inv.bank.length + inv.bankBags.reduce((n, b) => n + b.items.length, 0)})`);
      // Bank: BankItemSlots 39-66 (28 squares, 7 wide in game); bank bags in 67-73.
      bagWindows(d, 'Bank', inv.bank, 28, 39, 7, inv.bankBags, 67, 7);
      f.append(d);
    }
    for (const [key, title, items] of [['keyring', 'Keyring', inv.keyring],
                                       ['currency', 'Currency', inv.currency],
                                       ['other', 'Other items', inv.other]]) {
      if (!items.length) continue;
      const d = collapsible(key, `${title} (${items.length})`);
      itemRows(d, items, (it) => `bag ${it.bag} / ${it.slot}`);
      f.append(d);
    }

    f.append(talentSection(c.talents || []), reputationSection(c.reputation_panel || []),
             achievementSection(c.achievements || []));

    const top = body.scrollTop;
    ItemTip.hide();
    body.replaceChildren(f);
    body.scrollTop = top;
  }

  function stamp() { status.textContent = 'updated ' + new Date().toLocaleTimeString(); }

  function markSelected() {
    for (const e of document.querySelectorAll('.pl')) e.classList.toggle('sel', e.dataset.name === name);
  }

  async function refresh() {
    if (!name) return;
    const mine = ++seq, who = name;
    try {
      const r = await fetch('/api/character/' + encodeURIComponent(who));
      const text = await r.text();
      if (mine !== seq || who !== name) return;
      if (!r.ok) {
        lastJson = null;
        head.replaceChildren(el('h2', null, who));
        body.replaceChildren(el('div', 'empty', r.status === 404 ? 'Character not found' : 'error ' + r.status));
        stamp();
        return;
      }
      if (text !== lastJson) {
        const c = JSON.parse(text);
        renderHead(c);
        renderBody(c);
        lastJson = text;
      }
      stamp();
    } catch (e) {
      if (mine === seq) status.textContent = 'error: ' + e;
    }
  }

  function open(who) {
    if (who !== name) {
      name = who;
      lastJson = null;
      head.replaceChildren(el('h2', null, who));
      body.replaceChildren(el('div', 'empty', 'loading…'));
      body.scrollTop = 0;
      status.textContent = '';
    }
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    // The page re-fits the map on resize; reuse that after the layout shifts.
    document.body.classList.add('inspect-open');
    dispatchEvent(new Event('resize'));
    markSelected();
    return refresh();
  }

  function close() {
    if (!name) return;
    name = null;
    lastJson = null;
    seq++;
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('inspect-open');
    dispatchEvent(new Event('resize'));
    markSelected();
  }

  document.getElementById('inspect-close').onclick = close;
  addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  // Click-away: anything outside the drawer, except the controls that open it
  // (list, markers) and the map toolbar, closes it. The map reports its own
  // clicks (the page calls Inspect.close()), because the DOM click that ends a
  // pan or a zoom-button press must not close the drawer.
  document.addEventListener('click', (e) => {
    if (name && !e.target.closest('#inspect, #list, #map, .marker, .bar')) close();
  });
  // Deep link: /#inspect=<name> opens the drawer, handy for offline characters.
  // A malformed hash must not take the rest of the page down with it.
  try {
    const m = location.hash.match(/^#inspect=(.+)$/);
    if (m) open(decodeURIComponent(m[1]));
  } catch (e) {
    console.warn('invalid inspect hash:', e);
  }

  return {open, close, refresh, current: () => name};
})();
