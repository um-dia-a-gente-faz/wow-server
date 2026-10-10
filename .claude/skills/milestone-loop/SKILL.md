---
name: milestone-loop
description: Work one GitHub milestone with several agents at once. A coordinator reads the implementable issues on the board, dispatches N author agents (one worktree each), sends every PR to a separate reviewer agent that runs /code-review, applies the review fixes, and merges one PR at a time. Use for `/milestone-loop <milestone> <n> agents`, "work milestone 5 with 3 agents", or "loop the issues of M5".
---

# Milestone loop

You are the **coordinator**. You do not write feature code. You pick issues, dispatch
agents, route PRs to review, and merge. Read `CLAUDE.md` (working model, *Never*) and
`pr-workflow` first; every agent you dispatch follows them.

Invocation: `/milestone-loop <milestone> [<n> agents]`. `<milestone>` is a number (`5`),
a title (`M5 - Console v1: …`) or `Later - …`. Default `n` is 2, the cap is 4. Resolve it
with `gh api 'repos/um-dia-a-gente-faz/wow-server/milestones?state=open'`: the match is the
title starting `M<n> -`. Work that milestone only; when it is done, report and stop.

Repo `um-dia-a-gente-faz/wow-server`, board = org project 7.

## Roles

| Role | Who | Does | Never |
|---|---|---|---|
| Coordinator | this session | picks issues, dispatches, routes, merges, reports | writes feature code, reviews |
| Author | one `Agent` per issue, `isolation: "worktree"`, background | implements one issue, opens the PR, applies review fixes | reviews or merges |
| Reviewer | a fresh `Agent` per review round, never the author | runs `/code-review`, posts the review | pushes, edits, merges |

Authors and reviewers are separate agents so the author never reviews its own PR.
Reviewer agents cannot always spawn sub-agents; if `/code-review` cannot, the reviewer runs
its two axes (Standards, Spec) one after the other in its own session.

## The loop

Agents report back on their own, so react to each completion; there is no polling.
Call `ScheduleWakeup` (1800 s) only as a fallback in case a completion is lost.

### 1. Fill the slots

Slots free = `n` minus authors currently running. For each free slot take the next eligible
issue (lowest number first). Eligible means all of:

- open, in the milestone, board status *Todo*, no assignee, no open PR closing it
- **no open blocker**: the native *blocked by* relationship first
  (`gh api graphql` on `blockedBy`, see `CLAUDE.md`), then any `## Blocked by` line
- a single PR can finish it; an umbrella or ambiguous issue is reported to the owner, with
  your questions as an issue comment, and skipped
- does not overlap the files of an author already running (two agents editing the same module
  is how interface drift happened; hold the later issue back)

Move the issue to *In progress* on the board and assign nobody else to it.

### 2. Dispatch an author

Brief for each author (give it the issue number, not a summary):

- Read the issue in full with its comments, then `docs/AGENT-DIRECTION.md` for the area.
- Branch `feature/gh-<n>-<slug>` or `bugfix/gh-<n>-<slug>` from `origin/main`, never stacked.
- Develop the issue with the `/implement` skill (it drives `/tdd`; if the skill cannot be
  invoked from an agent, read `.claude/skills/implement/SKILL.md` and follow it). Keep the
  code minimal (`ponytail`); use `trinity-protocol` for any packet and `wowmap-dev` for
  `tools/wowmap`. `/implement` ends with a `/code-review` self-pass and a commit: that does
  not replace step 3, the separate reviewer still runs.
- Run `scripts/check.sh` before opening the PR.
- Open the PR from `.github/PULL_REQUEST_TEMPLATE.md`: title `gh-<n>: <summary>`,
  `Issue: gh-<n>`, `Closes #<n>`, and the marker `<!-- milestone-loop -->`. Watch CI to green,
  set the board to *In Review*.
- Anything it cannot verify (live VM, the owner in game) stays unticked under *How to test*.
- Return the PR number and a list of what is unverified. Do not review or merge.

### 3. Review

When an author returns a PR, dispatch a reviewer agent on it:

- Check out the PR branch in a worktree and run `/code-review main` (fixed point `main`).
- Post one comment in the `.github/REVIEW_TEMPLATE.md` shape (`## Review by <agent>`,
  `**Verdict:**`, Findings, Required fixes) with `gh pr comment`.
- Report the verdict back. No pushes, no edits, no merge.

### 4. Route the verdict

- `request-changes` → send the author (SendMessage to it, or a fresh author on the same
  branch) every item under *Required fixes*; it fixes, pushes, answers each point in a
  comment. Then dispatch a new reviewer. After **two** rounds without `approve`, stop and
  tell the owner.
- `approve`, or `comment` with no required fixes → merge queue.

### 5. Merge queue — one at a time

Merge only when all hold, otherwise leave the PR and say why:

1. A review from another agent, newer than the last push, with verdict `approve`.
2. CI green (`gh pr checks <n>`) and `mergeable` = `MERGEABLE`. If main moved, merge
   `origin/main` into the branch, rerun `scripts/check.sh`, wait for CI again.
3. No open change request and no "shelving" or "hold" comment.
4. **Acceptance criteria met**: every criterion in the issue is satisfied, or the remainder
   is split into its own issue first. `Closes` never covers work that was not delivered.
5. **No deploy path**: the diff does not touch `docker-compose.yml`, `scripts/`, worldserver
   config or the deploy workflow. A merge there recreates the worldserver and disconnects
   players, so ask the owner and wait for the go-ahead.
6. **Spacing**: `origin/main` has no commit from the last 10 minutes (another merge may
   be deploying). Otherwise wait.

Then `gh pr merge <n> --squash --delete-branch`. Confirm the issue closed and is *Done*,
remove the worktree, `git fetch origin`. One merge, then back to step 1 to refill the slot.

### 6. Finish

The milestone is done when every issue is closed, or the rest are skipped or blocked. Report
per issue: merged PR, left over and why, human steps still open, deploy-path PRs waiting for
the owner. Then stop; moving to another milestone is the owner's call.

## Never

- Never merge a PR the loop did not open, or one without another agent's `approve`.
- Never run more than `n` authors, or more than one merge at a time.
- Never push to `main`, force-push a shared branch, restart the worldserver, run
  `scripts/deploy.sh`, or use GM commands on agent characters.
- Never print or commit credentials.
- Never widen a PR: a new problem found on the way becomes its own issue in the right
  milestone.
- If anything is ambiguous or risky, stop and ask the owner.
