# Global chat feed spike

> **Superseded in part by UM-47 (2026-09-25).** The conclusion below — tail
> `Server.log` — does not work on the build this project runs. The deployed
> worldserver (3.3.5a, build 12340) writes **no player chat to any log**, so
> the tailer follows a file that never gets a chat line. `Logger.chat.log`
> did not produce output here, and enabling it would need a worldserver
> restart, which this project forbids.
>
> Chat is now taken **off the wire instead of out of the log**: an agent is
> already a protocol client receiving `SMSG_MESSAGECHAT`, and
> `agent/chat_relay.py` POSTs what it hears to `tools/chat-feed`'s
> `POST /api/chat/ingest`. `tools/chat-feed/app.py` still tails the log —
> it costs nothing and would work on a build that does log chat — but the
> relay is the real source.
>
> Still accurate below: the SSE transport choice, the event schema, and the
> privacy/escaping guidance (which the ingest path implements). The
> "Recommended implementation" step 1 and the effort estimate are not.

This note investigates roadmap item 4, **Global chat feed**, on TrinityCore
3.3.5a. It recommends a log-tail sidecar rather than adding a chat database table
or polling the character database.

## What TrinityCore can log

Stock TrinityCore 3.3.5a includes `src/server/scripts/World/chat_log.cpp`. Its
`ChatLogScript` writes chat at debug level to the `chat.log` logger hierarchy.
The stock `worldserver.conf.dist` has the relevant logger commented out:

```ini
# Existing appenders in the stock configuration:
Appender.Console=1,3,0
Appender.Server=2,2,0,Server.log,w

# Enable chat payloads at debug level and send them to stdout and Server.log.
Logger.chat.log=2,Console Server
```

Restart `worldserver` after changing logging configuration. `2` is Debug
(`LogLevel` in `LogCommon.h`: 1 trace, 2 debug, 3 info), which is necessary because
the script uses `TC_LOG_DEBUG`. An earlier version of this note said `3`, which
is Info and drops every chat line. `Appender.Server` writes
`Server.log` beneath `LogsDir` (or the process working directory if `LogsDir` is
empty); in this repository's container image, `/app/server/logs` is a named volume.
The image's own generated configuration should be inspected before editing: a
downstream build may rename appenders or expose an equivalent `Server.Chat`
setting. For unmodified upstream 3.3.5a, `Logger.chat.log`, not `Server.Chat`, is
the concrete setting.

The built-in script excludes addon-language messages. It logs these payload
shapes (the logging prefix/timestamp is supplied by the configured appender):

```text
Player Alice says (language 0): Hello
Player Alice yells (language 0): Help!
Player Alice emotes: waves
Player Alice tells guild Night Watch: Ready
Player Alice tells channel world: Looking for group
```

It also supports whispers, party, raid, battleground, officer chat, and system
channels. Custom `/world` implementations normally travel through a `Channel`,
so they should appear as `Player <name> tells channel <channel>: <message>` if
they use the normal channel path. Verify this on the deployed custom script; the
name is case-preserved and is not guaranteed to be literally `world`.

## Feasibility and constraints

A sidecar can follow the log with inode-aware `tail -F` behaviour, parse only
`chat.log.*` payloads, normalize them into a small event object, and fan out live
events. This is feasible without modifying the core or the characters schema.
It avoids scrape/cardinality problems: chat messages should not be Prometheus
metrics.

Parsing is necessarily version/configuration-sensitive because the appender owns
the timestamp and severity prefix. Treat the script payload after the logger
name as the stable contract, use anchored patterns per message shape, and emit an
`unknown` event (with a parse-error counter) for a newly observed shape rather
than dropping it silently. Do not split messages on `:` globally: message bodies
may contain colons, links, or UTF-8. Capture only the first delimiter after the
known sender/channel portion.

The feed is user-generated content. Keep it authenticated like the existing web
UI, make private channels opt-in or exclude them by default, escape content in
the browser, rate-limit each client, and bound the in-memory replay buffer.
`/say`, `/yell`, and public/custom channels are reasonable defaults; guild,
party, raid, officer, and whispers should be a deliberate privacy decision.

## Recommended implementation

1. ~~Enable `Logger.chat.log=2,Console Server` and test `/say`, `/yell`, guild,
   and the custom `/world` channel against the exact deployed `Server.log`.~~
   **Does not work on this build** — no chat reaches any log, and changing
   logging configuration needs a worldserver restart. Superseded by the
   agent relay (UM-47); see the note at the top of this file.
2. Add a small `chat-feed` sidecar that reads the mounted `server_logs` volume,
   tails `Server.log`, parses the known payloads, and keeps the last 100--500
   normalized public events in memory. Persisting chat is out of scope for the
   first version.
3. Serve `GET /api/chat/stream` as SSE from the sidecar (or proxy it through
   wowmap). On connect, send the bounded replay then live events. SSE is the
   preferred first transport: it is one-way, browser-native, reconnects with
   `Last-Event-ID`, and needs less connection management than WebSocket.
4. Add a simple feed consumer in the web UI. If interactive moderation or client
   subscriptions are later needed, retain the parser/event model and add a
   WebSocket adapter rather than changing the tailer.

Suggested event schema:

```json
{"id":"log-inode:offset","at":"2026-09-14T12:34:56Z","kind":"say","sender":"Alice","channel":null,"text":"Hello"}
```

As shipped in UM-47, both sources emit the same shape, with `target` (a
whisper addressee) and `source` (which agent relayed it) added, and `id`
assigned by the feed (`relay:<n>` for ingested events):

```json
{"id":"relay:3","at":"2026-09-26T01:19:22.378Z","kind":"channel","sender":"Shadowblade",
 "channel":"General - Eversong Woods","target":null,"text":"hello","source":"Shadowblade"}
```

Because several agents hear the same message, each relayed event carries a
`dedupe_key` derived from the message alone (kind, speaker GUID, channel,
target, text) — never from the listener — and the feed keeps the first copy
within `CHAT_FEED_DEDUPE_WINDOW_S`. The key is consumed by the feed and is
not part of the published event.

## Rough effort

One engineer can prototype configuration validation, a resilient tailer/parser,
SSE endpoint, tests using fixture log lines, and a minimal UI in roughly 3--5
days. Production hardening (authentication integration, privacy policy,
container restart/log-rotation tests, metrics/alerts, and moderation/retention
decisions) is another 3--7 days. A core change is unnecessary for the initial
public feed, but exact output must be captured from this project's image before
the parser is treated as production-ready.
