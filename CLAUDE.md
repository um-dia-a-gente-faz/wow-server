# CLAUDE.md

TrinityCore 3.3.5a WoW server (LAN-only) plus `agent/`, headless Python agents that
play on it with an LLM, and `tools/wowmap`, the internal observability site.
Linear (`UM-*`, team *Um Dia a Gente Faz*) is the tracker of record.

## Never

- **Never restart the worldserver** or run `scripts/deploy.sh`. Merging to `main`
  auto-deploys the VM within 5 minutes, which disconnects everyone playing.
- **Never use GM commands on agent characters** (`.go`, `.die`, `.modify`, …) and never
  modify accounts or the owner's character (Rubens). Relog or walk instead.
- **Never merge a PR**, force-push a shared branch, or push to `main`. Reviewing and
  merging belong to a separate reviewer agent (see *Working model*).
- **Never print, log or commit credentials.** They live in `.env` on the VM.
- **Never claim something works because the code looks right.** Verify against the
  TrinityCore source and, where it matters, against the live server.

## Always

- `agent/` is **stdlib-only** Python 3.12 (`urllib`, `struct`, `socket` — no pip deps).
  `tools/` may use `pymysql`/`Pillow`.
- Tests: `python3 -m unittest discover -s agent/tests` (also `tools/chat-feed/tests`,
  `tools/wowmap/tests`). CI runs compile, tests, compose config, gitleaks, image build.
- Branch `feature/<ref>-<slug>` or `bugfix/<ref>-<slug>`; **base always `main`, never
  stack** (see CONTRIBUTING.md for the outage this caused). `<ref>` is the issue the
  branch serves: `gh-<n>` for a GitHub-native issue, `UM-<n>` for one mirrored from
  Linear. A branch with no issue reference is not allowed.
- PR title `<ref>: <summary>`; body starts with `Issue: <ref>`, includes `Closes <ref>`
  so GitHub links the PR to its issue, and follows `.github/PULL_REQUEST_TEMPLATE.md`.
  Commits are Conventional Commits.
- **Every PR links its issue** — `Closes <ref>` when it finishes the work, `Refs <ref>`
  plus what remains otherwise. The board's *Linked pull requests* field depends on it.
- Anything you could not verify goes under "How to test" as a human step. Don't tick it.

## Working model

Two roles, kept apart. Every PR is opened and merged under the same GitHub account
(`Cividati`), so the role decides who may merge, not the account.

- **Author agent (default, you):** writes the code, runs the tests, opens the PR to
  `main`, keeps CI green, rebases when it conflicts, and answers review comments.
  It stops there and never approves or merges a PR, not even one that looks ready.
- **Reviewer agent:** a separate session the owner starts. It reviews, checks
  mergeability, and merges with `gh pr merge --squash --delete-branch` when the PR
  is well tested: CI green, no conflicts, tests pass after merging main into it,
  the template is followed, no open change requests, and wire formats checked
  against the TrinityCore source.
- **Every merge is a deploy** that disconnects players, so merges happen one at a
  time and only on purpose.
- **PRs can depend on each other.** When one changes a signature another PR also
  uses, say so in both PR bodies. The PR that merges second carries the follow-up.
- **Shelved PRs stay open** until the owner decides. A coordinator comment saying
  "shelving" means don't extend or merge it.
- **The board is the operating picture** (org project *wow-server*, project 7): the
  author moves the issue to *In progress* when the branch starts and to *Review* when
  the PR is open with CI green; *Done* belongs to the reviewer on merge. Author
  sessions never merge, so they never move an issue to *Done*.

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
