# CLAUDE.md

TrinityCore 3.3.5a WoW server (LAN-only) plus `agent/`, headless Python agents that
play on it with an LLM, and `tools/wowmap`, the internal observability site.
GitHub issues and the org project *wow-server* (project 7) are the tracker of record.
Linear (`UM-*`) is the older mirror and is no longer kept up to date.

## Never

- **Never restart the worldserver** or run `scripts/deploy.sh`. Merging to `main`
  auto-deploys the VM within 5 minutes, which disconnects everyone playing.
- **Never use GM commands on agent characters** (`.go`, `.die`, `.modify`, …) and never
  modify accounts or the owner's character (Rubens). Relog or walk instead.
- **Never merge a PR** unless it is your own, another agent has reviewed it, CI is green
  and no change request is open (see *Working model*). Never force-push a shared
  branch or push to `main`.
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
- **Every issue is linked to a PR, so every PR says `Closes <ref>`** — never `Refs`.
  GitHub only links on a closing keyword, and the board's *Linked pull requests* field
  reads that link. If live or human steps remain, keep `Closes`, leave them unticked
  under "How to test" and note that the issue is to be reopened if they fail (see
  CONTRIBUTING.md). An issue that cannot have a PR yet (blocked on an unmerged PR,
  needs a live run, deferred) gets it once unblocked; never stack to get the link.
- Anything you could not verify goes under "How to test" as a human step. Don't tick it.

## Working model

Two roles, kept apart. Every PR is opened and merged under the same GitHub account
(`Cividati`), so the role decides who may merge, not the account.

- **Author agent (default, you):** writes the code, runs the tests, opens the PR to
  `main`, keeps CI green, rebases when it conflicts, and answers review comments.
  It never approves a PR and never merges one it did not open. It merges **its own**
  PR only once a reviewer agent has reviewed it, the review's fixes are applied, CI is
  green, there are no conflicts and no open change requests (the `milestone-loop`
  skill does this end to end).
- **Reviewer agent:** a separate session the owner starts. It reviews, checks
  mergeability, and may merge with `gh pr merge --squash --delete-branch` when the PR
  is well tested: CI green, no conflicts, tests pass after merging main into it,
  the template is followed, no open change requests, and wire formats checked
  against the TrinityCore source. The author never reviews its own PR.
- **Every merge is a deploy** that disconnects players, so merges happen one at a
  time and only on purpose.
- **PRs can depend on each other.** When one changes a signature another PR also
  uses, say so in both PR bodies. The PR that merges second carries the follow-up.
- **Shelved PRs stay open** until the owner decides. A coordinator comment saying
  "shelving" means don't extend or merge it.
- **The board is the operating picture** (org project *wow-server*, project 7): the
  author moves the issue to *In progress* when the branch starts and to *In Review* when
  the PR is open with CI green; *Done* is set by whoever merges, which closes the
  issue through `Closes #<n>`.

### Milestones and board naming

**Milestones are named `M<n> - <subject>`.** Worked example (the plan as of 2026-10-05):

| # | Title | Track |
|---|---|---|
| 1 | `M2 - Human-like player` | agent |
| 2 | `M3 - Jev decision brain` | agent |
| 7 | `M4 - CI & live-test pipeline` | agent/infra |
| 3 | `M5 - Console v1: map + character panel` | console |
| 4 | `M6 - Console v2: map + inventory` | console |
| 5 | `M7 - Console v3: agent control` | console |
| — | `M8 - Architecture & maintainability` | engineering |
| 6 | `Later - Autonomy + multi-agent` | unscheduled bucket |

1. `<n>` is the milestone's place in the plan, not its creation order. A new milestone
   takes the next free `M`-number; never reuse one.
2. The console track keeps `Console vN` **inside the subject**, so older references
   (`milestone:console-vN-…` labels, "Console vN" in issue bodies) stay meaningful.
3. The unscheduled bucket is `Later - <subject>`, deliberately without a number: a number
   would imply a position in the plan it does not have.
4. **The GitHub milestone is the scope of record.** The `milestone:*` labels are a legacy
   mirror, kept because older board views still filter on them. Renaming a milestone does
   not rename its label and the label slug stays as it is; do not delete or rename those
   labels.
5. **Renaming a milestone breaks every board view that filters on its title.** Renaming
   `M3 — Jev decision brain` to `M3 - Jev decision brain` silently emptied view 8, which
   filtered on `milestone:"M3 — Jev decision brain"`. Pair every milestone rename with an
   update to the views that reference it, and check the views afterwards.
6. **Dependencies go in GitHub's issue relationships**, not prose: set *blocked by* /
   *blocking* on the issue (GraphQL `addBlockedBy`), so the relationship is machine-readable
   and the `milestone-loop` skill can check it. Keep a `## Blocked by` line in the body only
   as the explanation.

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
- `docs/PROTOCOL-NOTES.md` — verified 3.3.5a wire formats. (The old handoff
  document, with its wrong layout table, was removed.)
- `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md` — services, ports, module map, how the VM is deployed.
- `docs/adr/` — decisions of record (session split, router, config schema, threading model).

## Skills (`.claude/skills/`)

`live-agent-test` (test on the live server) · `trinity-protocol` (wire formats) ·
`pr-workflow` (ship a change) · `milestone-loop` (work a milestone end to end, merge
after another agent's review) · `wowmap-dev` (the observability site).
