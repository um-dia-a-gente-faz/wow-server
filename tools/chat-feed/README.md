# Chat-feed sidecar prototype

This is the prototype recommended by [the global-chat-feed spike](../../docs/CHAT_FEED_SPIKE.md)
for [ROADMAP item 4](../../docs/ROADMAP.md). It follows the TrinityCore
`Server.log` file, normalizes ChatLogScript payloads, keeps a bounded in-memory
replay buffer, and publishes public messages using Server-Sent Events (SSE).

## TrinityCore logging prerequisite

The compose service attempts the image's documented dot-to-double-underscore
mapping with `TC_WORLD__Logger__chat__log=3,Console Server`. The underlying
TrinityCore setting is `Logger.chat.log=3,Console Server`, but logger keys are
a special case because they themselves contain dots. Inspect the generated
`worldserver.conf` on this image and verify that override before deployment; if
it is not written exactly, configure the setting directly in the image's
supported worldserver configuration mechanism. Restart worldserver after it is
enabled.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOG_PATH` | `/logs/Server.log` | Path to TrinityCore's `Server.log`. |
| `REPLAY_BUFFER_SIZE` | `200` | Number of public events kept for reconnects. |
| `LISTEN_PORT` | `9500` | HTTP listen port. |
| `CHAT_FEED_CHANNELS` | `say,yell,channel` | Comma-separated public kinds. |

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

## Run and test

```sh
docker compose up -d --build chat-feed
python3 -m unittest discover -s tools/chat-feed/tests
python3 -m py_compile tools/chat-feed/app.py tools/chat-feed/tests/test_parser.py
```

## Prototype limitations

This is intentionally the 3--5 day prototype scoped by the spike, not a
production service. It has no authentication or reverse-proxy integration,
per-client rate limiting, durable history, moderation workflow, retention
policy, metrics/alerts, or deployment validation against the live image's exact
logger format. Rotation handling is implemented but has not been exercised
against every Docker/runtime rotation mode. Capture real `Server.log` payloads
before treating the parser as a stable contract.
