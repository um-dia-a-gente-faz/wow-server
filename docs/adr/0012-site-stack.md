# ADR 0012: Console v4 site stack (FastAPI, SvelteKit, SQLite, SSE)

## Status

Proposed (#311), docs only. Records the decisions of the Console v4 design review
(spec #310). Nothing in this ADR changes code. Open questions are collected at the end.

ADR numbers: `0011` is already taken on `main` by the agent memory store, so the
site-stack ADR is 0012 and the GM-control ADR is 0013. Issue #311 and #310 still call
them 0011 and 0012.

## Context

The observability site (`tools/wowmap`) is a read-mostly set of Python pages. The owner
can watch the map and inspect characters, but cannot control agents, set missions, see an
agent's own view of the world, or see Jev token use, without a terminal. The site must
also work on a phone as well as a PC, and the owner wants newer technology, a real backend
and a database for project data that is not game data (spec #310). `tools/wowmap` is split
into modules already (ADR 0008) and talks to the agents through the versioned agent API
(ADR 0010).

## Decision

- **Backend: FastAPI.** It replaces the current page-serving code over time. It exposes a
  versioned JSON API, an SSE stream for live data and a generated OpenAPI spec. Game data
  comes from a read-only, pooled, short-TTL-cached query layer. The existing coordinate
  transform, map-art and model pipelines are reused, not rewritten.
- **Frontend: SvelteKit with TypeScript.** Responsive and installable as a PWA, mobile-first
  for the agent list and controls, desktop-first for the map. Types are generated from the
  OpenAPI spec and never hand-copied. SvelteKit was chosen for lightness and the owner's wish
  for newer technology. The backend contract is framework-independent, so the frontend can be
  replaced without touching the API.
- **Own store: SQLite** on a volume, separate from the game database. It holds admin users,
  mission assignments, unmet intents, the control action log, token usage history and a
  reserved presence-mode field (not exposed yet). Postgres is adopted only if concurrent
  writers appear. Project data can never touch characters or accounts.
- **Live updates: SSE**, so the map and logs refresh without polling.
- **Migration: one page at a time** behind the existing site port. The old pages keep working
  until each new page replaces them, so the migration never leaves a gap.
- **Roles.** Viewer (no login, read-only) and Admin (login, session cookie, hashed password).
  The first admin is created by a one-time command-line step, so no default password exists.
  What admins may do is in ADR 0013.
- **Build.** The frontend build is part of CI and of the container image.
- **Secrets stay on the backend.** The provider key and balance lookup are server-side; the
  browser only receives numbers.

## Alternatives rejected

- **React / Next.js.** Not chosen. Issue #311 names it as rejected; spec #310 records
  SvelteKit's lightness and the owner's wish for newer technology as the reasons for the
  choice, and no argument against React or Next.js (see Open questions).
- **Bun + Hono.** Not chosen. Same: issue #311 names it as rejected, #310 gives no reason
  against it (see Open questions).
- **Keeping the current Python page-serving site.** Rejected as the end state: the owner
  wants a real backend, a database for project data and a phone-friendly installable
  frontend. It stays in place during the migration.
- **Postgres for the own store.** Deferred, not rejected: SQLite until concurrent writers
  appear.

## Consequences

- Two runtimes (Python backend, Node build for the frontend) and a generated-types step
  between them. CI and the image must build both.
- The site's own store is a second database next to the game database. The game database is
  opened read-only; nothing in the site writes to it.
- The agent API (ADR 0010) stays the only way the site reaches an agent, and the OpenAPI
  spec plays the same role between the backend and the frontend.
- The unset agent API URL and the character-stats setting are fixed as part of the
  migration (spec #310), not by this ADR.

## Open questions

1. Why React/Next.js and Bun+Hono were rejected. #311 names them and #310 gives only the
   reasons for choosing SvelteKit; #309 does not discuss the site stack. The owner should
   add the reasons, or confirm that "lightness and newer technology" is the whole answer.
2. Hash algorithm and parameters for admin passwords, session lifetime, cookie flags and
   whether state-changing requests need CSRF protection. #310 says only "session cookie,
   hashed password".
3. Where the SQLite file lives (volume path) and how it is backed up. #310 says "on a volume".
4. How the PWA is served: by the FastAPI process or by a separate static server behind the
   same port.
