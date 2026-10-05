// Chat comes from tools/chat-feed's SSE stream on a different port; wowmap only
// renders it (see window.CHAT_FEED_URL, set in index.html). Sender names and message text are
// untrusted and always rendered with textContent, never innerHTML.
const Chat = (() => {
  const KIND_LABEL = {say: 'says', yell: 'yells', channel: 'channel', guild: 'guild',
    party: 'party', raid: 'raid', officer: 'officer', battleground: 'battleground',
    whisper: 'whisper', unknown: '?'};
  const list = document.getElementById('chat');
  const status = document.getElementById('chat-status');
  let source = null, autoscroll = true, backoffMs = 2000;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function append(ev) {
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
    const row = el('div', 'chat-msg kind-' + (ev.kind || 'unknown'));
    if (ev.sender) {
      const sender = el('span', 'sender', ev.sender);
      sender.onclick = () => window.selectCharacter && window.selectCharacter(ev.sender);
      row.append(sender, ' ');
    }
    if (ev.channel) row.append(el('span', 'chan', `[${ev.channel}] `));
    row.append(document.createTextNode(ev.text || ''));
    list.appendChild(row);
    while (list.children.length > 300) list.removeChild(list.firstChild);
    if (autoscroll || atBottom) list.scrollTop = list.scrollHeight;
  }

  function feedUrl() {
    if (window.CHAT_FEED_URL) return window.CHAT_FEED_URL.replace(/\/$/, '') + '/api/chat/stream';
    return `${location.protocol}//${location.hostname}:9500/api/chat/stream`;
  }

  function connect() {
    if (source) return;
    status.hidden = false;
    status.textContent = 'connecting to chat…';
    try {
      source = new EventSource(feedUrl());
    } catch (e) {
      status.textContent = 'chat unavailable: ' + e;
      return;
    }
    source.addEventListener('chat', (e) => {
      try { append(JSON.parse(e.data)); } catch (err) { /* malformed event, skip */ }
    });
    source.onopen = () => { status.hidden = true; backoffMs = 2000; };
    source.onerror = () => {
      status.hidden = false;
      status.textContent = 'chat disconnected, reconnecting…';
    };
  }

  function disconnect() {
    if (source) { source.close(); source = null; }
  }

  list.addEventListener('scroll', () => {
    autoscroll = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
  });

  return {connect, disconnect, shown: () => !list.hidden};
})();
