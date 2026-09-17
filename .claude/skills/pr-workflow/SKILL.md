---
name: pr-workflow
description: Ship a change in this repo end to end — branch naming, commits, the PR template, CI, and the Linear bookkeeping (attach the PR, set status, record what a human still has to verify). Use when starting work on a UM issue, opening or updating a PR, or reviewing someone else's.
---

# Shipping a change

Details live in `CONTRIBUTING.md`; this is the loop.

## Before writing code

1. Read the Linear issue in full: description, acceptance criteria, comments (owner
   decisions often live in a comment and override the description), and its agent brief.
2. Check `docs/AGENT-DIRECTION.md` for constraints on the area you're touching.
3. Branch from `main`: `feature/UM-<n>-<slug>` or `bugfix/UM-<n>-<slug>`.
   Use a worktree for parallel work: `git worktree add ../wowwork-<slug> -b feature/UM-<n>-<slug>`.

## While working

- **Base is always `main`. Never stack a PR on another feature branch.** A stacked PR
  merges into its base, and if that base was squash-merged earlier, the content silently
  never reaches `main` (this really happened — see CONTRIBUTING.md → Branches).
  Describe dependencies in the PR body instead.
- Conventional Commits (`feat(agent): …`, `fix(wowmap): …`).
- Parallel work on the same files is common here. Before opening the PR, check what
  landed on `main` meanwhile and whether another open PR expects a different interface
  than the one you built (one PR read `session.inventory` while another exposed
  `world.build_equipment_and_inventory()`; after both merged, the feature was dead).
- Run the tests you can: `python3 -m unittest discover -s agent/tests`, plus
  `tools/chat-feed/tests` and `tools/wowmap/tests` when you touched those.

## Opening the PR

- Title `UM-<n>: <brief summary>`; body from `.github/PULL_REQUEST_TEMPLATE.md`, starting
  with `Issue: UM-<n>`.
- Fill "How to test" with commands someone can actually run, and paste live-run log
  excerpts when the change is protocol-level.
- **Anything you couldn't verify stays unticked and is listed as a human step**
  (e.g. "needs the owner in game", "needs a GM command", "needs a server restart").
  Never tick a box you didn't check.
- Wait for CI (`gh pr checks <n> --watch`). Green before you call it done.

## Linear bookkeeping

- Attach the PR link to the issue and leave it **In Progress**. Don't mark it Done —
  the owner merges and closes.
- Comment on the issue with what you verified live and what is still open.
- File new problems you found as their own issue in the right milestone rather than
  widening this PR's scope.

## Never

- Never merge your own PR, and never push to `main`.
- Never force-push a branch someone may have based work on; never delete a branch another
  PR uses as its base.
- Never restart the worldserver or run `scripts/deploy.sh` — merging auto-deploys.
