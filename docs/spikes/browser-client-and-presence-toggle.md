# Browser-run client and the presence toggle spike (#338)

> **Research spike.** Outcome: do not build or adopt a browser client now; ship the
> presence toggle without it. Issue #338, milestone *Later - Autonomy + multi-agent*.
> Related: [live view in the site](live-view-in-the-site.md) (#337).
> Researched 2026-10-18, read-only. Every external claim
> carries a link; anything not checked against a primary source is marked **Unverified**.

## Question

Could the owner play 3.3.5a in a browser, what would a separate repository hold, and does the
deferred "agents play while the owner is away" toggle really depend on that? (#338.)

## Short answer

1. **Feasibility: partial at best.** The only open-source browser WoW client aimed at 3.3.5a,
   Wowser, authenticates, lists realms and characters and enters the world, but renders
   terrain and models only experimentally and its last source change (`src/`) is from 2016-07-27; the v0.0.2 release (2020-03-20) only bumped package metadata and the 2021 commit touches only the README. Nothing found
   is a playable browser client.
2. **Legal limit:** every project needs the user's own copy of the client files, and Blizzard's
   EULA restricts derivative works and unauthorized servers. A design must keep assets on the
   owner's machine or LAN. Not legal advice.
3. **The toggle does not need a browser client.** It needs a place to store the mode, a
   controller that pauses and resumes agents, and a way to know the owner is online. The
   only real link is "present" without a desktop PC. **Owner decision needed:** the toggle was
   deferred until the game is playable in a browser (#338); this spike finds no technical
   dependency, so building it first reverses that recorded deferral. Keep the deferral or
   lift it?
4. **Recommendation:** if the owner lifts the deferral, build the toggle first, on the agent API; do #337 stage 1 (a rendered
   view of agent state) in the site; revisit a browser client only if the owner needs to play
   from a device with no Windows client. Rough size if ever built: very large.

## Existing projects

Checked with the GitHub REST API on 2026-10-18 (licence, last push) and each project's own
README.

| Project | What it is | State | Licence |
|---|---|---|---|
| [wowserhq/wowser](https://github.com/wowserhq/wowser#readme) (umbrella) | "World of Warcraft in the browser using JavaScript and WebGL"; 3.3.5a only | Proof of concept; being split into client and pipeline; package v0.0.2; last source change (`src/`) 2016-07-27; v0.0.2 release (2020-03-20) only bumped package metadata (CHANGELOG, package.json); last commit 2021-02-12 is README only | MIT |
| [wowserhq/client](https://github.com/wowserhq/client#readme) | TypeScript, vite, WebGL 2 web client for Blizzard UI files | Pushed 2025-10-08. README: "extremely primitive scene rendering" (frames and textures mostly), "no BLP support yet" | MIT |
| [wowserhq/pipeline](https://github.com/wowserhq/pipeline#readme) | Node asset server: serves client MPQ data over HTTP, BLP to PNG, DBC to JSON | Pushed 2018; MPQ clients only | MIT per README; GitHub reports `NOASSERTION` |
| [Deamon87/WebWowViewerCpp](https://github.com/Deamon87/WebWowViewerCpp#readme) | C++ map and model viewer with a WebGL build used at wow.tools/mv | Active (pushed 2026-09-14). A viewer with no game protocol; its README describes opening a local CASC storage, not 3.3.5a MPQs | GitHub reports no licence |
| [Deamon87/WebWoWViewer](https://github.com/Deamon87/WebWoWViewer#readme) | Earlier JS viewer targeting WotLK | Last push 2017 | GitHub reports none |
| [magcius/noclip.website](https://github.com/magcius/noclip.website) | Level "museum" with a `src/WorldOfWarcraft` scene | Active (pushed 2026-10-06). A viewer with no server connection | GitHub reports `NOASSERTION` (custom) |

What Wowser's own README says it can do today: authenticate by username and password, list
realms, connect to a realm, list characters, join the world, and log world packets, with "good
progress" on terrain and models. It warns against use on retail servers.

### What is missing, by layer

| Layer | Status in the browser | Evidence |
|---|---|---|
| Transport | Browsers have no raw TCP. Wowser opens `ws://` and depends on `websockify` to reach the realm's TCP ports | [`src/lib/net/socket.js`](https://github.com/wowserhq/wowser/blob/master/src/lib/net/socket.js), [`package.json`](https://github.com/wowserhq/wowser/blob/master/package.json). The general platform rule is **Unverified** here |
| Auth (SRP6) | Done in JS, wrapping the third-party `jsbn` library (`jsbn/lib/big-integer`, `jsbn/lib/sha1`) for big numbers and SHA1; see also [`big-num.js`](https://github.com/wowserhq/wowser/blob/master/src/lib/crypto/big-num.js) | [`src/lib/crypto/srp.js`](https://github.com/wowserhq/wowser/blob/master/src/lib/crypto/srp.js) |
| World protocol | Partial: connect, characters, enter world, packet logging. Movement, combat, spells, quests and inventory are not in its feature list | Wowser README, *Features* |
| Rendering | ADT, M2 and WMO loaders exist in [`src/lib/pipeline`](https://github.com/wowserhq/wowser/tree/master/src/lib/pipeline); README says only "good progress". The UI client is at frames and textures | Wowser and client READMEs |
| Asset delivery | A server converts MPQ data; the browser never reads MPQs. The server needs StormLib and BLPConverter | Wowser README *Development*; pipeline README |
| Audio, input, UI | Audio is named among the challenges; UI (XML/Lua) lives in the early client repo | Wowser README *Background* |

Compared with this repo, the protocol side is smaller than it looks: `agent/` already does
auth, world login, movement and combat in stdlib Python, checked against 3.3.5a
(`docs/PROTOCOL-NOTES.md`). A browser client would have to port that to TS or sit behind a
gateway.

### Legal limits on Blizzard assets

- Wowser and its client both say the official client is required, that the pipeline serves
  resources over HTTP that "may be available to others", and "do not distribute game data you
  do not own" ([Wowser README](https://github.com/wowserhq/wowser#readme),
  [client README](https://github.com/wowserhq/client#readme)).
- Blizzard's [End User License Agreement](https://www.blizzard.com/en-us/legal/fba4d00f-c7e4-4883-b8b9-1b4500a402ea/blizzard-end-user-license-agreement)
  prohibits creating "derivative works based on or related to the Platform" and "any
  connection to any unauthorized server that emulates, or attempts to emulate, the Platform".
  Whether that reaches a LAN-only 3.3.5a server is a legal question this spike does not
  settle. **Unverified** beyond the quoted text.
- Design rule: assets stay on the owner's machine or LAN, are never committed or published,
  and are converted on demand from the owner's own MPQs. The repo already works this way for
  the inspect-drawer body model (`docs/spikes/CHARACTER-MODEL-SPIKE.md`, #171: M2, skin and
  BLP read from the VM's client, vendored three.js, no build step).

## What a separate repository would hold

Why separate: a browser client is TypeScript with its own build chain and release cadence and
its own asset-licensing concerns, while `agent/` is stdlib-only Python 3.12 (CLAUDE.md).

| In the new repo | Stays in this repo |
|---|---|
| Browser client (render, input, UI shell) | TrinityCore server, `scripts/`, compose, deploy |
| Protocol layer in TS (or a fork of Wowser's) | `agent/` and `docs/PROTOCOL-NOTES.md`, the source of truth for wire formats |
| Asset pipeline (MPQ to web formats), run locally, outputs never in git | `tools/wowmap` site, its model endpoints and the site store (ADR 0012) |
| WebSocket-to-TCP gateway code or config | The compose service that exposes a gateway, if ever added (it touches `docker-compose.yml`, so owner approval per CLAUDE.md) |

Boundary: the new repo reaches this one only through the realm's ordinary ports (via the
gateway) and, if embedded in the site, the site's versioned API (ADR 0010, ADR 0012). It
imports no code from `agent/`; wire facts are copied from the protocol notes, not linked by
code. The gateway is a new network surface on the LAN and needs the same care as any
admin-adjacent service (ADR 0013).

## How the presence toggle depends on a browser client

**Source of the requirement.** The condition that the toggle waits for
a playable browser game is recorded in #338 only. [ADR 0012](../adr/0012-site-stack.md)
records only that the field is "reserved ... not exposed yet". Spec #310
(story 41 and *Out of Scope*) lists live view, the browser WebAssembly client and the toggle
as research issues, with "only the data field" reserved. `docs/AGENT-DIRECTION.md` has no
entry on it (searched for presence, away, toggle, browser). The reason for tying it to browser
play is not written down anywhere found; the reading below is an **inference** to confirm
with the owner.

**Reading.** "Agents play while I am away" implies the opposite mode, "agents hold back while
I am here". Today the owner joins with a desktop 3.3.5a client
(`docs/AGENT-DIRECTION.md` *The goal*, `docs/CLIENT-SETUP.md`).

| Piece | Needs a browser client? |
|---|---|
| The mode value (`present` / `away`) and its storage | No |
| Pausing or resuming agent brains by mode | No. #310 story 26 designs a pause and resume control on the agent API |
| Knowing the owner is online | No. `characters.characters.online` is the presence signal, per `docs/LIVE-MAP.md` (line 73) |
| The owner being "present" without a desktop PC | Yes. This is the only real link |
| Defining "away" (logged out vs. idle) | A product decision, not a client |

So the toggle can ship with the desktop client. A browser client matters only if "present"
must be possible from a device that cannot run Wow.exe, and even then the field and logic are
the same.

## What the presence-mode field in the site store already prepares

Checked against `origin/main`.

**It prepares:** the decision to keep a presence mode in the site's own SQLite store,
separate from the game DB ([ADR 0012](../adr/0012-site-stack.md), *Own store*), so the later
feature has a data home and no extra store.

**It does not prepare, and is not built:**

- **The field in code.** Searches for `presence_mode` and `presence-mode` match nothing. The
  Console v4 store from ADR 0012 is not in the tree yet; the only SQLite store under
  `tools/wowmap` is the activity feed (`ACTIVITY_DB`, `tools/wowmap/README.md`), which has no
  such field.
- **Values and meaning.** Which modes exist, what each makes agents do (stop, idle near the
  owner, keep playing), and whether the setting is global or per agent.
- **An enforcer.** Something must read the mode and tell agents. The Tier 1 controls are a
  design in #310, not a presence-driven policy.
- **Detection.** Polling `characters.online` and handling relogs.
- **API, UI and audit.** No endpoint, no site control, no log entry; a pause and resume toggle
  needs its own admin-gated route (ADR 0013 covers GM actions, not this).
- **Anything browser-specific.** The field is client-agnostic.

## Options and sizes

Rough person-effort estimates, not measured. **Unverified**; no primary source exists.

| Option | What | Size |
|---|---|---|
| A. Presence toggle on the agent API | Store field, admin-gated endpoint, poller on `characters.online`, pause and resume wiring, one site control | S to M, after the ADR 0012 store exists |
| B. #337 stage 1, rendered agent view | See [live view in the site](live-view-in-the-site.md); reuses the #171 three.js model | M |
| C. Try Wowser as is | Run its pipeline on the owner's MPQs plus a websockify gateway | S to try; the result is a login demo, not a game |
| D. Finish a browser client (Wowser or fresh) | Full world protocol, movement, combat, spells, UI, terrain, WMO and M2 rendering, audio, input, asset pipeline | XL, many weeks to months; Wowser has not reached playability |

## Recommendation

1. **Owner decision first:** A reverses the recorded deferral (#338: wait until the game is
   playable in a browser). If the owner lifts it, do **A** once the Console v4 store exists. First settle the open product questions (modes,
   global vs. per agent, definition of away) in the issue. Keep it client-independent.
2. Do **B** for visibility; it covers watching from a phone far more cheaply than D.
3. Do not start **D** unless the owner needs to play from a device with no Windows client. If
   so, time-box **C** first to measure what renders today. Wowser is the only open-source
   starting point found and is MIT licensed, but its core is stale.
4. Keep any browser repo out of this one (table above) and never commit or publish assets.

## Not verified

- No client was run; project states come from READMEs, the GitHub API and source trees.
- Whether Wowser still builds or its renderer works today.
- The general rule that browsers cannot open raw TCP (no primary source fetched).
- Whether the EULA applies to this LAN setup, and the owner's reason for the deferral.
- All size estimates.
- Discovery used GitHub repository search with several phrasings; other projects may exist.
