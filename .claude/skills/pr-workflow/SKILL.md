---
name: pr-workflow
description: Ship a change in this repo end to end — branch naming, commits, the PR template, CI, and the board bookkeeping (link the PR, set status, record what a human still has to verify). Use when starting work on an issue, opening or updating a PR, or reviewing someone else's.
---

# Shipping a change

Details live in `CONTRIBUTING.md`; this is the loop.

## Before writing code

1. Read the GitHub issue in full: description, acceptance criteria, `## Blocked by`, and
   comments (owner decisions often live in a comment and override the description).
2. Check `docs/AGENT-DIRECTION.md` for constraints on the area you're touching.
3. Branch from `main`: `feature/gh-<n>-<slug>` or `bugfix/gh-<n>-<slug>` (`UM-<n>` only for an issue
   mirrored from Linear).
   Use a worktree for parallel work: `git worktree add ../wowwork-<slug> -b feature/gh-<n>-<slug> origin/main`.

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

- Title `#<n>: <brief summary>`; body from `.github/PULL_REQUEST_TEMPLATE.md`, starting
  with `Issue: #<n>`.
- Every issue is linked to a PR, so the body says `Closes <ref>` — never `Refs`, which
  links nothing. If live or human steps remain, keep `Closes`, leave them unticked
  and add the note from `CONTRIBUTING.md` (issue is to be reopened if they fail).
  Verify the link with GraphQL `closingIssuesReferences`; edit a body with
  `gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -F body=@file` when `gh pr edit`
  fails, then re-check the issue's board status (*In Review*).
- Fill "How to test" with commands someone can actually run, and paste live-run log
  excerpts when the change is protocol-level.
- **Anything you couldn't verify stays unticked and is listed as a human step**
  (e.g. "needs the owner in game", "needs a GM command", "needs a server restart").
  Never tick a box you didn't check.
- Wait for CI (`gh pr checks <n> --watch`). Green before you call it done.

## Board bookkeeping

- The issue is *In progress* while you write it and *In Review* once the PR is open with
  CI green. Merging closes it through `Closes #<n>`, which moves it to *Done*.
- Comment on the issue with what you verified live and what is still open.
- File new problems you found as their own issue in the right milestone rather than
  widening this PR's scope.

## Never

- Never merge a PR that is not yours, or one no other agent has reviewed, or one with red
  CI or open change requests. Never push to `main`. To merge your own reviewed PR, follow
  `milestone-loop`.
- Never force-push a branch someone may have based work on; never delete a branch another
  PR uses as its base.
- Never restart the worldserver or run `scripts/deploy.sh` — merging auto-deploys.
