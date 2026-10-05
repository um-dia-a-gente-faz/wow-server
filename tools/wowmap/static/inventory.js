// Money as the game's coins (#177). The one place copper is split and drawn: the item
// tooltip, the drawer and the activity feed all call Coins.render. Zero denominations are
// dropped, a zero purse shows `0` copper, the title keeps the raw copper for the DB.
const Coins = (() => {
  const NAMES = {g: 'gold', s: 'silver', c: 'copper'};
  const whole = (copper) => Math.max(0, Math.floor(Number(copper) || 0));
  // 12345 -> [1, 23, 45] (gold, silver, copper)
  function split(copper) {
    const c = whole(copper);
    return [Math.floor(c / 10000), Math.floor(c / 100) % 100, c % 100];
  }
  function render(copper) {
    const c = whole(copper);
    const out = document.createElement('span');
    out.className = 'coins';
    out.title = `${c.toLocaleString('en-US')} copper`;
    const parts = split(c).map((n, i) => [n, 'gsc'[i]]).filter(([n]) => n);
    if (!parts.length) parts.push([0, 'c']);
    out.setAttribute('role', 'img');  // a bare span can't carry a name; img does, and hides the digits
    out.setAttribute('aria-label', parts.map(([n, d]) => `${n} ${NAMES[d]}`).join(' '));
    for (const [n, d] of parts) {
      const num = document.createElement('span');
      num.textContent = d === 'g' ? n.toLocaleString('en-US') : String(n);
      const coin = document.createElement('span');
      coin.className = 'coin ' + d;
      coin.setAttribute('aria-hidden', 'true');
      out.append(num, coin);
    }
    return out;
  }
  return {split, render};
})();

// In-game style item tooltip (UM-80). The API sends ready-made lines
// (item_tooltip.py); this only draws them, with textContent, and keeps the box on screen.
const ItemTip = (() => {
  const TIP_COLORS = {white: '#ffffff', green: '#1eff00', yellow: '#ffd100', gray: '#9d9d9d',
    red: '#ff2020'};
  const QUALITY = ['#9d9d9d', '#ffffff', '#1eff00', '#0070dd', '#a335ee', '#ff8000', '#e6cc80', '#e6cc80'];
  const tip = document.createElement('div');
  tip.className = 'item-tip';
  tip.hidden = true;
  tip.setAttribute('role', 'tooltip');
  document.body.append(tip);
  let anchor = null;

  function span(cls, text) {
    const e = document.createElement('span');
    if (cls) e.className = cls;
    e.textContent = String(text);
    return e;
  }
  function render(it) {
    const lines = it.tooltip && it.tooltip.length ? it.tooltip : [{left: it.item_name, color: 'quality'}];
    tip.replaceChildren();
    for (const l of lines) {
      const row = document.createElement('div');
      row.className = 'tl';
      row.style.color = l.color === 'quality' ? (QUALITY[it.quality] || '#fff') : (TIP_COLORS[l.color] || '#fff');
      const left = span('l', l.left);
      if (l.money !== undefined) left.append(' ', Coins.render(l.money));
      row.append(left);
      if (l.right) row.append(span('r', l.right));
      tip.append(row);
    }
  }
  // Beside the square (left first: the drawer sits on the right), else below or
  // above it; always clamped to the viewport so narrow screens keep it visible.
  function place() {
    const r = anchor.getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight, vw = innerWidth, vh = innerHeight, m = 6;
    let x = r.left - w - m, y = r.top;
    if (x < m) x = r.right + m;
    if (x + w > vw - m) {
      x = Math.max(m, Math.min(r.left, vw - w - m));
      y = r.bottom + m;
      if (y + h > vh - m) y = r.top - h - m;
    }
    y = Math.max(m, Math.min(y, vh - h - m));
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }
  function show(box, it) {
    anchor = box;
    render(it);
    tip.hidden = false;
    place();
  }
  function hide() { tip.hidden = true; anchor = null; }
  function attach(box, it) {
    box.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') show(box, it); });
    box.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && anchor === box) hide(); });
    box.addEventListener('focus', () => show(box, it));
    box.addEventListener('blur', () => { if (anchor === box) hide(); });
    // Touch: a tap shows it; tapping anywhere else (or scrolling) hides it.
    box.addEventListener('click', () => show(box, it));
  }
  document.addEventListener('pointerdown', (e) => {
    if (anchor && !anchor.contains(e.target)) hide();
  });
  addEventListener('scroll', () => { if (anchor) hide(); }, true);
  addEventListener('resize', () => { if (anchor) hide(); });
  return {attach, hide};
})();

