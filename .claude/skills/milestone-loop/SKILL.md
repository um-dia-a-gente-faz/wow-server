---
name: milestone-loop
description: Work one GitHub milestone of wow-server unattended with several agents. An Opus coordinator picks the unblocked issues, dispatches Haiku author agents (one worktree and one PR per issue), sends every PR to an Opus reviewer that runs the code-review skill, routes the fixes, and squash-merges one PR at a time. Use for `/milestone-loop <milestone> [<n> agents]`, "loop milestone 5", or "work M5 with 3 agents".
---

# Milestone loop

You are the **coordinator**, running on Opus. You pick issues, dispatch agents, route PRs
to review, and merge. Feature code and reviews belong to the agents you dispatch.

The loop runs **unattended**: the owner is not watching. Decide what you can decide, record
each decision where the next reader will find it, and keep the other issues moving. Only a
*park* (below) waits for the owner, and a park never stops the loop.

Invocation: `/milestone-loop <milestone> [<n> agents]`. `<milestone>` is a number (`5`) or a
title (`M5 - Console v1: …`); resolve with `gh api 'repos/um-dia-a-gente-faz/wow-server/milestones?state=open'`; the match is the open milestone whose title starts
`M<n> -`. Default `n` is 2, the cap is 3. Work that milestone only.

Repo `um-dia-a-gente-faz/wow-server`. Board = org project 7, with Status
*Todo → In progress → In Review → Done*.

## Roles

| Role | Who | Does |
|---|---|---|
| Coordinator | this session, Opus | picks issues, decides, dispatches, routes, merges, logs |
| Author | one `Agent` per issue: `subagent_type: "loop-author"` (Haiku), `isolation: "worktree"`, background | implements one issue, opens the PR, applies review fixes |
| Reviewer | a fresh `Agent` per review round: `subagent_type: "loop-reviewer"` (Opus), `isolation: "worktree"` | runs the `code-review` skill, posts the review |

The two agent types are defined in `.claude/agents/`. Each carries its standing brief and
only the tools it needs, which keeps its starting context small. Your dispatch message
therefore holds only what is specific to the issue: its number, the branch, and the checks
or cautions that apply to this change. If a type is not
available in the session, use `general-purpose` with the same `model` and paste the brief
from the agent file. For the ladder, pass `model: "sonnet"` or `model: "opus"` to
`loop-author`.

An author's PR is always reviewed by a different agent. Authors are Haiku: they do what
the issue and your message say and little more, so precision goes into the issue.

## Park

A **park** is the one way work waits for the owner. Park an issue or PR when moving it
forward needs a secret, a repository setting or visibility change, a paid service, a
deploy-path change, or a choice that is hard to undo. To park: add the label `needs-owner` (create it if missing), comment with the exact
decision needed and your recommendation, write it in the log, and move on. An issue
labelled `needs-owner` is ineligible until the owner removes the label.

## Log

One issue per milestone, titled `Loop log: <milestone title>`, labelled `loop-log` (create it if missing), outside
the milestone and the board. Find it or create it. Comment on it at every start, dispatch,
verdict, decision, park and merge — one or two lines each, with issue and PR numbers. The
log is what the owner reads instead of this terminal.

## Effort

The board has one number field, **Estimate**. Fill it after the work, in thousands of
tokens, as the total spent building the issue.

Every agent completion reports its token total (`subagent_tokens`). Keep a tally per issue
and per model: each author and each reviewer that worked on it, across all rounds. The
figure for a resumed agent is cumulative, so count that agent's latest figure once. When the
issue's PR merges, set Estimate to the total divided by 1000, rounded, and put the split in
the merge line of the log (`effort 237k: Haiku 90k, Opus 147k`). A parked PR gets its tally
so far. The coordinator's own tokens are not counted; say so in the final report, and give
the milestone totals per model there.

After a restart the tally for work in flight is lost; record the part you can see and mark
the log line `effort ≥ <n>k`.

## Start

1. **Fetch `main`.** Never push to it: every merge deploys. Leave local changes alone and
   note them in the log; authors work from `origin/main` in their own worktrees.
2. **Reconcile.** A previous run may have died mid-flight. For every milestone issue that
   is *In progress* or *In Review*, find its PR or branch (`*/gh-<n>-*`) and re-enter the
   loop at the matching step:
   - open PR, no review newer than the last push → step 3
   - open PR, latest verdict `request-changes` → step 4
   - open PR, latest verdict `approve` → step 5
   - a pushed branch without a PR → dispatch an author to finish it on that branch
   - nothing → set the issue back to *Todo*
3. **Announce** in the log: the milestone, what was reconciled, which issues are eligible
   now, which are held back and why, and `n`.

## The loop

Agents report back on their own; react to each completion. Call `ScheduleWakeup` (1800 s)
only as a fallback for a lost completion.

### 1. Fill the slots

Slots free = `n` minus authors running. For each free slot take the next eligible issue,
lowest number first. Eligible means all of:

- open, in the milestone, board status *Todo*, not labelled `needs-owner`, no open PR
  closing it
- every blocker is closed: the native `blockedBy` relationship, then any `Blocked by #<n>`
  in the issue body
- it touches different files from every author already running; otherwise hold it back

Before dispatching, make the issue *ready* yourself. A ready issue is a complete spec a
Haiku author can follow without designing anything: the files to create or change, the
exact exported names and signatures, the behaviour rule by rule, the dependencies to add,
and a **Done when** list where every item names the test or command that proves it. Read the code on `main` that the issue builds on, then:

- **Thin** → write the missing spec into the body.
- **Unclear, or out of date with `main`** → decide from `GLOSSARY.md`, `docs/adr/` and the
  code, and post the answer as an issue comment starting `Decision:`. A later Decision
  wins over an earlier one and over the body. A decision that is hard to undo is a park.
- **Unverifiable** → rewrite any **Done when** item an agent cannot check into one a test
  or command can check.
- **Too large for one PR** → split it into smaller issues in the same milestone, each with
  its own **Done when** and `Blocked by` lines.

Keep the slots busy when issues queue behind one shared file:

- **Split out the pure part.** A module with no UI (a store, a file writer, a data table)
  becomes its own issue and runs in parallel; the original keeps the wiring and is blocked
  by it.
- **Batch small overlaps.** Two issues that each add a line or two to the same file may
  run together when you tell each author exactly where its lines go and what the other one
  is touching. The second PR to merge then takes `origin/main` and is re-reviewed.
- An issue that rewires a shared file runs alone.

Move the issue to *In progress*.

### 2. Dispatch an author

The `loop-author` agent carries the standing brief. Your message adds only:

- the issue number and the branch `<type>/gh-<n>-<slug>`
- which `Decision:` comment is authoritative, and the files to read in full first
- what a parallel author is editing, and the exact places this author may edit in a
  shared file
- the trap in this issue, if you can see one

When the PR is open, move the issue to *In Review*. Turn each out-of-scope problem the
author reports into a new issue in the milestone it belongs to. If the author reports an
edge your own spec created, correct the spec with a Decision and send the same author back
before spending a review on it.

### 3. Review

The `loop-reviewer` agent carries the standing brief, including testing the tests by
breaking the implementation. Your message adds the PR and issue numbers and the
checks specific to this PR:

- **Name the failure that matters most** for this change (a wrong packet layout, a
  disconnected player, a stuck agent, lost data) and ask for a hunt for it, with scenarios.
- **Ask for evidence nobody has yet.** Wire formats are checked against the TrinityCore
  source, not the author's code. A `tools/wowmap` change is run against the data and looked
  at. Data copied from a table is compared with the table independently of the author's
  tests. What needs the live VM or the owner in game stays an unticked human step.
- **Say what earlier rounds settled.** From round 2 on the reviewer verifies the fixes and
  leaves accepted matters closed.
- **Narrow the review to what changed.** A merge of `origin/main` gets a check of the
  merge; a round with no new commits (a Decision removed the requirement) gets a check of
  the Decision and the PR body, with no checkout.

### 4. Route the verdict

`approve` → merge queue. `request-changes` → the author gets every item under *Required
fixes*, each with the acceptance that proves it and the instruction to see its new test
fail first. The author fixes, pushes, and answers each item in a PR comment; then a new
reviewer looks.

A fix round carries the reviewer's required fixes and nothing else. A note the reviewer
did not require becomes a new issue or a Decision. A required fix that needs a design
choice gets a Decision from you first; when the choice is to drop a requirement, say so in
a Decision, correct the PR body, and move the work to its own issue.

The author climbs a ladder as rounds fail, each new author on the same branch with the full
review history:

| Review round | Author |
|---|---|
| 1 and 2 | Haiku (`SendMessage` to the original, or a fresh one) |
| 3 | `model: "sonnet"` |
| 4 | `model: "opus"` |

A PR still rejected after round 4 is parked with the reviewer's standing objections, and
its slot is freed for another issue.

### 5. Merge queue — one at a time

Merge when all hold:

1. An `approve` from a reviewer agent, newer than the last push.
2. CI ran on the head commit and `gh pr checks <n>` is all green. No checks is a failure,
   not a pass: the repository plan has no branch protection, so this rule is the gate.
3. `mergeable` is `MERGEABLE`. If `main` moved and the branch conflicts, the author merges
   `origin/main` into the branch and the PR goes back to step 3. A clean, conflict-free
   update needs only green CI again.
4. Every item in the issue's **Done when** list is delivered, or the remainder is split
   into its own issue first.
5. The PR was opened by this loop (marker `<!-- milestone-loop -->`), follows
   `.github/PULL_REQUEST_TEMPLATE.md`, has `Closes #<n>`, and carries no "shelving" or
   "hold" comment or open change request.
6. **No deploy path.** The diff does not touch `docker-compose.yml`, `scripts/`, worldserver
   config or the deploy workflow. A merge there recreates the worldserver and disconnects
   players: park the PR.
7. **Spacing.** `origin/main` has no commit from the last 10 minutes (another merge may be
   deploying). Otherwise wait.

Then `gh pr merge <n> --squash --delete-branch` with the PR title as the subject. Confirm
the issue closed, set it to *Done*, set its Estimate, remove the agents' worktrees and
their local branches (`worktree-agent-*` and the PR branch), `git fetch --prune origin`, and
return to step 1.

### Interruptions

An agent that ends with a usage-limit or API error has not reported: its work may be half
done. Stop dispatching, write the state to the log, and stop. On resume, run Start again;
Reconcile finds the open PRs and branches.

When the owner says to wind down, finish the PRs already open (review, fixes, merge), start
no new issue, and post the report of step 6 for what was done.

### 6. Finish

The milestone is done when every issue is closed or parked, or blocked behind a park. Post
the final report to the log, per issue: merged PR, or what is parked and the decision it
needs. Send a `PushNotification` with the one-line outcome and the log link. Then stop;
the next milestone starts when the owner asks for it.

Before the report, leave the machine clean: no worktree, no local branch and no container
or preview server left by an agent (`ss -ltnp` shows a server an agent failed to stop; check
its working directory before stopping it), and CI green on the last commit of `main`.

## Limits

- At most `n` authors and one merge at a time.
- `main` changes only through a reviewed, squash-merged PR. Never push to `main`,
  force-push a shared branch, or merge a PR this loop did not open.
- Never restart the worldserver, run `scripts/deploy.sh`, use GM commands on agent
  characters, or touch accounts.
- Secrets stay out of commits, PRs and comments.
