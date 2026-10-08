# Chat-feed sidecar prototype

This is the prototype recommended by [the global-chat-feed spike](../../docs/spikes/CHAT_FEED_SPIKE.md)
for [ROADMAP item 4](../../docs/ROADMAP.md). It follows the TrinityCore
`Server.log` file, normalizes ChatLogScript payloads, keeps a bounded in-memory
replay buffer, and publishes public messages using Server-Sent Events (SSE).

## TrinityCore logging prerequisite

The root compose file sets `TC_WORLD__Logger__chat__log=2,Console Server`,
which the image writes into `worldserver.conf` as `Logger.chat.log = 2,Console Server`
(verified on the live image). Chat lines come from TrinityCore's `ChatLogScript`,
which logs at **debug**, so the level must be `2` (`LogLevel`: 1 trace, 2 debug,
3 info). At `3`, every chat line is dropped and this feed shows nothing.
Changing the value recreates the worldserver container on the next deploy,
which disconnects everyone online.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOG_PATH` | `/logs/Server.log` | Path to TrinityCore's `Server.log`. |
| `REPLAY_BUFFER_SIZE` | `200` | Number of public events kept for reconnects. |
| `LISTEN_PORT` | `9500` | HTTP listen port. |
| `CHAT_FEED_CHANNELS` | `say,yell,channel` | Comma-separated public kinds. |
| `CHAT_FEED_TOKEN` | empty | Shared secret for `GET /api/chat/stream`. Empty leaves the stream open to the LAN. |
| `CHAT_FEED_MAX_CLIENTS` | `100` | Stream clients connected at once; further ones get `429`. `0` = no cap. |
| `CHAT_FEED_MAX_CLIENTS_PER_IP` | `20` | Same cap per client address. `0` = no cap. |

Known private kinds (`guild`, `party`, `raid`, `officer`, and `whisper`) are
parsed but excluded by default. Add one deliberately to `CHAT_FEED_CHANNELS`
only after deciding its privacy policy. Unknown `Player ...` payload shapes are
published as a sanitized `unknown` event and increment `parse_errors` in
`/healthz`; their raw text is intentionally not exposed.

The service starts at the end of an existing log, so its replay is from the
sidecar's lifetime, not a historical log import. It detects both truncation and
inode replacement while following the path.

## Browser consumer

```html
<script>
  const feed = new EventSource("http://localhost:9500/api/chat/stream");
  feed.addEventListener("chat", ({ data }) => {
    const event = JSON.parse(data);
    // Use textContent, not innerHTML: chat text is untrusted user input.
    console.log(`[${event.kind}] ${event.sender ?? "system"}: ${event.text}`);
  });
</script>
```

Browsers automatically reconnect and send `Last-Event-ID`; the sidecar replays
the events after that ID when it is still in its bounded buffer.

Every response (including `/healthz` and the SSE stream) sends
`Access-Control-Allow-Origin: *`, so a page served from wowmap's origin
(`:9400`) can call `EventSource()` against this service (`:9500`) directly —
see the chat tab in `tools/wowmap`'s console page. There is nothing
sender-writable here (this is a read-only tailer), so an open CORS policy adds
no write surface.

## Run and test

```sh
docker compose up -d --build chat-feed
python3 -m unittest discover -s tools/chat-feed/tests
python3 -m py_compile tools/chat-feed/app.py tools/chat-feed/tests/test_parser.py
```

## Stream token and client caps

The stream is open by default, like the rest of this LAN-only stack. Setting
`CHAT_FEED_TOKEN` makes `GET /api/chat/stream` require the token, either as
`Authorization: Bearer <token>` or as `?token=<token>` (a browser's
`EventSource` cannot set headers). A missing or wrong token gets `401`. The
token is compared in constant time and is never logged. `/healthz` and
`/metrics` carry no chat text and stay open, so Prometheus needs no secret.

A token in a URL ends up in browser history and proxy logs, so treat it as a
LAN guard, not as real authentication. The two consumers in this repo do not
send a token yet: the chat tab in `tools/wowmap` (it would need
`window.CHAT_FEED_URL` to include `?token=`) and wowmap's activity feed
(`ACTIVITY_CHAT_FEED_URL`). Turn the token on only after wiring those.

Each stream client holds a thread and a socket, so connections are capped in
total and per client address. Over the cap the answer is `429`, and
`chat_feed_clients_rejected_total` goes up. Behind Docker's port proxy several
browsers can share one source address, which is why the per-address default is
generous.

## Metrics

`GET /metrics` serves Prometheus text format (stdlib, no client library):

| Metric | Type | Meaning |
| --- | --- | --- |
| `chat_feed_events_total{kind}` | counter | Events published to the stream. Every kind in `CHAT_FEED_CHANNELS` has a series from the start, at 0. |
| `chat_feed_parse_errors_total` | counter | Log lines that looked like chat but matched no known shape. |
| `chat_feed_ingest_duplicates_total` | counter | Relayed events dropped because another agent already reported them. |
| `chat_feed_ingest_rejected_total` | counter | Relayed events dropped as unusable. |
| `chat_feed_clients` | gauge | Stream clients connected now. |
| `chat_feed_clients_rejected_total` | counter | Stream connections refused by a cap. |
| `chat_feed_log_rotations_total` | counter | Times the tailed log was truncated or replaced. |
| `chat_feed_last_event_timestamp_seconds` | gauge | Unix time of the last published event, 0 before the first. |

Counters reset when the container restarts. The scrape job to add on VM201
(`/opt/pandora/prometheus/prometheus.yml`) is listed in
`monitoring/docker-compose.yml`'s header.

## Log rotation

The tailer (`LogTailer`) reopens the file when its inode changes or its size
drops below the read position, and reads the new content from the start.
`tests/test_hardening.py` runs it against a real temp file for both rotation
modes: truncation in place (`copytruncate`) and replacement (rename, then a new
file). It also covers a partial line, a file that disappears and comes back,
and event ids staying distinct across a replacement. These pass on Linux/ext4
and tmpfs; a live run against the container's `server_logs` volume is still a
manual step.

## Limitations

There is no durable history, moderation workflow or retention policy, and no
alert rules or dashboard panel yet for the metrics above. The stream token is a
shared secret, not per-user auth, and nothing here terminates TLS.

The log tailer finds no chat on this server build: TrinityCore here never
writes player chat to `Server.log`, even with the logger override above (see
`docs/AGENT-DIRECTION.md`, known findings). The real source is the agents'
relay, `POST /api/chat/ingest`. So there are no real `Server.log` chat lines to
capture as fixtures; `tests/fixtures/chat.log` stays the `ChatLogScript` format
taken from the TrinityCore source.
