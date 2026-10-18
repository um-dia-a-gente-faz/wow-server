# Live view in the site spike (#337)

> **Spike.** Outcome: build the rendered radar (option 1), do not build streaming, defer control. Issue #337. Superseded by: nothing.

Issue #337, milestone "Later - Autonomy + multi-agent". Research only, no code. Read
2026-10-18 against `main` at `3c06cb1`.

**Recommendation in one line:** build option 1 (a rendered view from data we already
have), starting with the radar already specced in #328; treat option 2 as an owner-side
convenience that needs no repo work beyond a link; defer option 3 to the Tier 1 control
work. Nothing found here changes the headless-agent decision.

Source marks: **[repo]** is a file in this repo, **[ext]** an external primary source with a
link, **[unverified]** a claim I could not check.

## Constraints from the decisions of record

- Agents are headless protocol clients: no game client, no rendering, no screen reading.
  "A game client per agent needs a GPU and a window"; headless agents cost about 50 MB of
  RAM each. The rejected list names screenshots plus input simulation and reading `Wow.exe`
  memory. [repo: `docs/AGENT-DIRECTION.md`, decision 1]
- The site is FastAPI + SvelteKit with SSE for live data, migrated one page at a time; the
  game data layer is read-only; secrets stay on the backend. [repo: `docs/adr/0012-site-stack.md`]
- Controlling agents from the site is admin-only, in two tiers. Tier 1 is ordinary player
  actions delegated through the agent's API. Tier 2 is GM-level debug control, confirm-gated
  and logged, and teleport stays disabled until `MSG_MOVE_TELEPORT_ACK` exists. [repo:
  `docs/adr/0013-admin-only-gm-control.md`]
- Issue #310 asks for "an agent's own view of the world" (user stories 20 and 21: a radar
  with typed objects, facing and target). #328 is that radar: "It shows perception, not
  database truth." [repo: issue #310, #328]

## Option 1: a rendered view built from data we already have

A browser view of one agent: its position and surroundings, drawn from the perception
snapshot, over the existing map art, with the 3D character model at the centre.

### What it needs

1. A per-agent feed of the snapshot to the browser (SSE per ADR 0012).
2. A 2D top-down renderer (the radar of #328) on the map art already served.
3. Optionally a 3D scene: the agent's model, plus simple markers for nearby units.

### What exists

- Perception snapshot: `agent/perception/snapshot.py` composes `nearby_units`,
  `nearby_players`, `nearby_objects` (`agent/perception/nearby.py`). Each unit carries guid,
  entry, name, type, distance, position (map, x, y, z), faction, level, `health_pct`,
  `target_guid`, `quest_giver_status`, `in_combat`. Served read-only at `GET /perception`
  (`agent/http_api.py`).
- Map art, coordinate transform, calibration: `tools/wowmap/wowmap/transform.py`,
  `calibration.py`, `assets.py`, `static/map.js` (Leaflet).
- Character model: `tools/wowmap/wowmap/static/charview.js` (`window.CharView.mount`),
  vendored `three.min.js` (r159), `models.py`, `pipeline/extract_models.py`; the API gives
  `model` via `state.model_url` (`tools/wowmap/wowmap/character.py`). Verified go in
  `docs/spikes/CHARACTER-MODEL-SPIKE.md`. Body only, rigid, no animation.
- Equipment on the model is open as #510 (draw it, or label the viewer body-only). Skin
  only: no face, hair or facial features. [repo: `CHARACTER-MODEL-SPIKE.md`, "Known limits"]
- Site-side SSE consumer pattern: `tools/wowmap/wowmap/activity.py` (`iter_sse`).
- Walk control and the proxy to the agent runner: `tools/wowmap/wowmap/walk.py`,
  `agent/control.py`.

### Gaps found

- **Facing is not in the snapshot.** `position_dict` in `agent/perception/nearby.py` unpacks
  `map_id, x, y, z, *_o` and drops the orientation, and I found no `facing`/`orientation` key
  anywhere in `agent/perception/`. #328 needs the agent's facing; that is a small
  agent-side change plus an `api_schema.json` entry (`agent/api_schema.json`,
  `agent/api_contract.py`). Whether the world state keeps the agent's own orientation
  reliably was not checked [unverified].
- **Snapshot is pull, not push.** `GET /perception` is polled today (the Agent tab polls
  every 3 s, `tools/wowmap/README.md`). A "live" feel needs the site backend to poll the
  agent at about 1 Hz and fan it out over SSE, or the agent to expose an SSE endpoint. The
  former needs no agent change.
- **No terrain or building geometry.** The view is a radar over 2D map art, not a 3D world.
  Rendering terrain would mean parsing ADT/WMO files, which no code here does; I did not
  size that. Treat a 3D world as out of scope for this option.
- **Perception range.** The snapshot is what the agent has seen through update packets, not
  everything nearby; this is the intended truth for a "what does the agent see" view.

### Standards used

- SSE is a W3C/WHATWG standard with a built-in `EventSource` client and automatic reconnect
  with `Last-Event-ID`: [HTML Standard, Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html).
- WebGL (what `three.js` uses) is documented at
  [MDN WebGL API](https://developer.mozilla.org/en-US/docs/Web/API/WebGL_API); the repo
  already degrades to "3D model unavailable" without it.

### Risks

- Staleness reads as truth: the view must show the snapshot age, or it will lie when an agent
  is mid-reconnect. (Cheap to mitigate.)
- Phone performance for the 3D part (#328 requires a phone-sized viewport). The 2D radar is
  safe; keep 3D to the inspect drawer as it is today.
- Naked models (#510) make a "live view" look wrong; ship the radar first, which does not
  depend on #510.
- Load on agents: one extra `/perception` poll per watched agent, only while a viewer has
  the page open.

### Size

- S: add facing to the snapshot and schema (agent side, tested).
- S/M: backend poll-and-fan-out SSE endpoint per agent, in the FastAPI backend (#323), tested at the HTTP
  seam with a fake snapshot, as #328 asks.
- M: radar renderer (typed, facing, target, phone viewport), which is #328 itself.
- M (optional): the model at the radar centre with facing, after #510 lands.

**Prerequisite:** #328 is blocked by the open #324 (SvelteKit shell), #326 (character panel)
and #288. The SSE fan-out belongs in the Console v4 FastAPI backend (ADR 0012), whose shell and
SSE stream are #323; it is not a new endpoint in the old `tools/wowmap` pages. So the real
order is: #323, #324, #326 and #288 land first, then the three slices above. Total: about 3 to
4 PR-sized slices on top of that prerequisite; the first useful one is the radar (#328).
The facing change is agent-side and can go in at any time.

## Option 2: stream the owner's own game client

The owner (Rubens) runs a real 3.3.5a client already, since he plays. Showing that picture in
the site is a screen-capture problem, not a protocol problem. It shows only the owner's
character, never an agent's, so it is a different feature from option 1.

### What it needs

- A capture/encode source on the owner's PC and a way to carry it to a browser. Two shapes:
  - **Sunshine + Moonlight**: Sunshine is "a self-hosted game stream host for Moonlight",
    with hardware encoding on AMD, Intel and NVIDIA GPUs and software encoding as a fallback
    ([Sunshine README](https://github.com/LizardByte/Sunshine),
    [docs](https://docs.lizardbyte.dev/projects/sunshine/latest/)). Moonlight is the client
    ([moonlight-qt](https://github.com/moonlight-stream/moonlight-qt)). The Sunshine docs
    describe a web UI for configuration and pairing only. I found no browser playback client
    in those docs [unverified: a third-party web client may exist]. So this serves a native
    Moonlight app, not an embed in the site.
  - **OBS to WebRTC to the browser**: OBS can publish over WHIP, "a new protocol that enables
    broadcasting via WebRTC" ([OBS WHIP guide](https://obsproject.com/kb/whip-streaming-guide)).
    WHIP is an IETF standard ([RFC 9725](https://www.rfc-editor.org/rfc/rfc9725.txt)). A media
    server turns it into something a browser can play. MediaMTX accepts WebRTC, RTSP, SRT and
    others and serves them to browsers ([MediaMTX README](https://github.com/bluenviron/mediamtx)).
    The page would then embed a `<video>` or WebRTC player.
- Alternatively, the browser's own capture: `getDisplayMedia` captures a display as a media
  stream, but the W3C spec is a working draft and the document itself says it is not complete
  ([Screen Capture, W3C](https://www.w3.org/TR/screen-capture/)). That captures the viewer's
  own screen, which is not what we want here.

### What exists in this repo

Nothing. No code under `agent/`, `tools/` or `scripts/` handles video. The site has no
embedding point for it, though a SvelteKit page (ADR 0012) could hold an `<iframe>` or
`<video>` cheaply. The VM is headless and LAN-only [repo: `CLAUDE.md`, "Where things are"], so
the stream would run from the owner's PC to the LAN browser, not through the VM.

### Risks

- It shows one character, the owner's. It does not answer "what is agent X seeing".
- Bandwidth and PC load on the owner's machine (encoder plus the game). Not measured.
- Latency: the OBS WHIP guide promises "interactive latency" with "only 100ms of delay"
  ([OBS WHIP guide](https://obsproject.com/kb/whip-streaming-guide)); Sunshine's README says
  only "low-latency" ([Sunshine README](https://github.com/LizardByte/Sunshine)) and gives no
  figure. Neither is measured on a LAN here. [unverified in this setup].
- Input capture is a different matter: viewing is safe, but Sunshine also emulates gamepads on the
  host from the client's input ([Sunshine docs](https://docs.lizardbyte.dev/projects/sunshine/latest/);
  keyboard and mouse forwarding is my inference, not read in those docs [unverified]),
  so exposing its port beyond the LAN would hand remote control of the owner's PC to whoever
  pairs. Keep it off the site; never publish it.
- The game's licence/terms for streaming were not checked [unverified]; for a private LAN
  realm I would expect no issue.

### Size

- S: a doc note on how the owner can stream with OBS or Sunshine/Moonlight (no repo code).
- M: an embedded player page behind a media server container (MediaMTX or similar), plus
  compose, auth and `docs/DEPLOYMENT.md` changes. Touching `docker-compose.yml` needs the
  owner's go-ahead per `CLAUDE.md`.

Not recommended as a site feature now: the cost is a new service for a view of one character.

## Option 3 (later): taking control

Two different things share this name.

### 3a. Directing an agent from the site (Tier 1)

Already decided: ordinary player actions delegated through the agent's API, admin-only,
logged. [repo: ADR 0013]

- Exists: `POST /control/walk` (`agent/control.py`), enabled only with `AGENT_CONTROL_TOKEN`,
  using the agent's own session and the same movement packets a client sends; the site's walk
  block (`tools/wowmap/wowmap/walk.py`, `static/walk.js`). No GM command, no teleport.
- Needs: more Tier 1 verbs (target, attack, use, cast, say), each through the existing action
  specs (`agent/action_specs.py`), and the control log in the site's SQLite store (ADR 0012).
- Interaction with the brain: decided, not open. ADR 0013 **Override** (and `GLOSSARY.md`
  **Override**, #310 stories 31 and 32): an override pauses the agent's brain and reflexes until
  the admin releases it, then the agent resumes its mission, and an agent under override is
  clearly marked. Today only the lock exists: the think loop holds `observer.action_lock` and a
  walk waits for the cycle, then blocks the next one (`agent/control.py`). The pause, the
  resume and the mark are not built; they are the first slice of this part.
- Risks: a half-driven agent confuses the brain's memory; teleport stays blocked by the
  missing `MSG_MOVE_TELEPORT_ACK`; a click-to-move on a radar needs a walkable-path check or
  the agent runs into walls (the walk endpoint reports `blocked`).
- Size: M per verb group, 3 to 5 slices for a useful set. Realtime WASD driving would be L and
  fights the movement heartbeat model [unverified: not prototyped].

### 3b. Controlling the game client itself

Remote input to the owner's client falls out of option 2 (Moonlight sends input). It is the
owner playing, not a site feature, and it carries the exposure risk above.

## Recommendation

1. **Do option 1 first**, once its prerequisites land (#328 is blocked by the open #324, #326 and
   #288; the SSE fan-out goes in the FastAPI backend, #323, per ADR 0012), in this order: facing
   in the snapshot (S), snapshot SSE fan-out (S/M), radar (#328, M). It uses only data and art the repo has, honors "perception, not database
   truth", works on a phone, and costs no new service. Add the 3D model at the centre only
   after #510.
2. **Option 2: do not build.** Offer the owner a short how-to (OBS/WHIP or Sunshine/Moonlight)
   if he wants to watch his own client; it shows one character and adds a media server.
3. **Option 3a after the radar**, as Tier 1 verbs, because the radar is the natural place to
   click. The first slice is the
   **override** already decided in ADR 0013 (brain and reflexes pause until release, the agent
   then resumes its mission, and it is marked while overridden). 3b is not a feature.

## What would change the headless-agent decision

Nothing here does. A rendered view needs only data the headless agent already holds.
Specifically, option 1 adds one field (facing) and no client.

The decision would be worth reopening only if one of these became true:

- The owner wants to see **the agent's true 3D view** (terrain, buildings, spell effects) as
  the game draws it. The only source of that is a real client per agent, which is the
  rejected path, or a server-side renderer that parses ADT/WMO/M2 data (a large new project,
  unsized).
- A feature needs information the protocol does not give a headless client but a real client
  has. I found none; the perception snapshot is built from the same update packets a client
  receives [repo: `agent/perception/`, `docs/AGENT-DIRECTION.md`].
- The fleet shrinks to a few agents on a machine with a GPU, which removes the stated cost
  reason (about 50 MB per agent vs a GPU and a window each). Not the case for the 25-agent target.

A single real client for a single "showcase" agent, streamed by option 2, would not reverse the
decision: it is an exception for one character, and the other agents stay headless.

## Unverified and open

- Whether Sunshine has a first-party browser player (docs read do not mention one).
- Real latency and CPU cost of any capture path on the owner's PC.
- Terms of use for streaming the 3.3.5a client.
- Whether the agent's own orientation is reliably tracked in `WorldState`.
- The MDN and W3C pages were fetched whole; I read only the passages cited.
