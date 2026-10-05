# ADR 0008: wowmap split into route table, repo layer and service modules

## Status

Accepted. Records a decision already merged in PR #257 (commit `f2066b2`, closes #249).

## Context

`tools/wowmap/app.py` had reached about 2900 lines (per the diffstat of
`f2066b2`): HTTP handling, every SQL statement, the page HTML, fog, calibration,
fleet proxying and character assembly in one file, with no unit that could be
tested without a socket.

## Decision

`app.py` is wiring only (about 120 lines: the `Handler` class, startup, and the
`ThreadingHTTPServer`). Responsibilities moved to:

| Module | Owns |
|---|---|
| `routes.py` | the route table: `(method, path pattern)` to handler, plus `dispatch(req)` |
| `webio.py` | socket-free `Request`/response types; a handler returns `(status, body)` or `(status, body, ctype, cache)`. `app.Handler` is the only code that touches a socket. |
| `repo/` (`players.py`, `characters.py`) | every SQL statement wowmap runs; returns plain rows |
| `players.py`, `character.py`, `inventory.py`, `areas.py` | services that turn rows into the API JSON |
| `agents.py`, `fleet.py`, `walk.py` | agent observability proxy, agent-runner proxy (token stays server-side), walk-to-point |
| `calibration.py`, `fog.py`, `fogview.py`, `assets.py`, `pages.py`, `state.py` | calibration store, fog of war, static assets, the single-page UI, process-wide config |

No URL or JSON shape changed.

## Consequences

- Handlers can be tested by calling them with a `webio.Request`; tests patch
  `state.db` or the repo functions (`tools/wowmap/tests/`).
- New SQL belongs in `repo/`, not in a service.
- `tools/wowmap/Dockerfile` lists the files it copies, so a new module has to be
  added there too (the split commit changed it for that reason).
- Follow-up (#262): `pages.py` no longer holds the front end. `static/index.html` is the page
  (`pages.py` fills in one value with `string.Template`); each panel has its own `static/*.css`
  and `static/*.js`, served by `assets.static`, with `node --test` tests in `tests/js/`.
- `activity.py` (the activity feed store and its sources) was not part of the
  split and is still its own large module.
