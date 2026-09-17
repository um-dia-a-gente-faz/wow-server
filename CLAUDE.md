# CLAUDE.md

TrinityCore 3.3.5a WoW server (LAN-only) plus `agent/`, headless Python agents that
play on it with an LLM, and `tools/wowmap`, the internal observability site.
Linear (`UM-*`, team *Um Dia a Gente Faz*) is the tracker of record.

## Never

- **Never restart the worldserver** or run `scripts/deploy.sh`. Merging to `main`
  auto-deploys the VM within 5 minutes, which disconnects everyone playing.
- **Never use GM commands on agent characters** (`.go`, `.die`, `.modify`, …) and never
  modify accounts or the owner's character (Rubens). Relog or walk instead.
- **Never merge your own PR**, force-push a shared branch, or push to `main`.
- **Never print, log or commit credentials.** They live in `.env` on the VM.
- **Never claim something works because the code looks right.** Verify against the
  TrinityCore source and, where it matters, against the live server.

## Always

- `agent/` is **stdlib-only** Python 3.12 (`urllib`, `struct`, `socket` — no pip deps).
  `tools/` may use `pymysql`/`Pillow`.
- Tests: `python3 -m unittest discover -s agent/tests` (also `tools/chat-feed/tests`,
  `tools/wowmap/tests`). CI runs compile, tests, compose config, gitleaks, image build.
- Branch `feature/UM-<n>-<slug>` or `bugfix/UM-<n>-<slug>`; **base always `main`, never
  stack** (see CONTRIBUTING.md for the outage this caused).
- PR title `UM-<n>: <summary>`; body starts with `Issue: UM-<n>` and follows
  `.github/PULL_REQUEST_TEMPLATE.md`. Commits are Conventional Commits.
- Anything you could not verify goes under "How to test" as a human step. Don't tick it.

## Where things are

| Thing | Where |
|---|---|
| Live realm | `192.168.1.64` (Proxmox host `pv1` at `192.168.1.75`), LAN only |
| Observability site | http://192.168.1.64:9400 (`tools/wowmap`) |
| Chat feed (SSE) | http://192.168.1.64:9500 (`tools/chat-feed`) |
| Agent decision logs | `/opt/wow-server-metrics/audit` on the VM |
| Secrets | `/opt/wow-server/.env` on the VM |

## Read before working

- `docs/AGENT-DIRECTION.md` — the owner's decisions and why. **Overrides older docs.**
- `CONTRIBUTING.md` — commits, branches, PRs, style, live-testing rules.
- `docs/PROTOCOL-NOTES.md` — verified 3.3.5a wire formats. (`docs/NEXT-AGENT-HANDOFF.md`'s
  old layout table is **wrong**; don't use it.)
- `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md` — services, ports, how the VM is deployed.

## Skills (`.claude/skills/`)

`live-agent-test` (test on the live server) · `trinity-protocol` (wire formats) ·
`pr-workflow` (ship a change) · `wowmap-dev` (the observability site).
