---
name: milestone-loop
description: Work GitHub milestones end to end as a senior dev, in a loop — pick an unblocked issue, branch from main, build it, open the PR, wait for another agent's review, apply its fixes, merge your own PR, repeat until the milestone is done, then move on to the next milestone. Use when asked to "work the milestone", "loop the issues of M3", or run `/milestone-loop <milestone>`.
---

# Milestone loop

One invocation is one **tick**. A tick does the single most useful next step, then
re-arms itself with `ScheduleWakeup` (300 s). Everything is derived from GitHub, so a
tick can be killed and restarted at any point. Read `CLAUDE.md` and `pr-workflow`
first; this skill only adds the loop and the merge rules.

Arguments: `[<start milestone title>] [idle=<n>]`. Default start `M3 - Jev decision brain`.
`idle` is the count of consecutive idle ticks; the loop carries it in its own re-arm prompt.

**Milestone order.** Sort milestones by their `M<n>` number ascending, `Later - …` last
(`gh api repos/um-dia-a-gente-faz/wow-server/milestones?state=open`). The **current
milestone** is the first one at or after the start milestone that still has open issues. When
it is finished the loop moves to the next one by itself; it never goes back before the start.
Finishing a milestone is never a reason to stop: the next milestone in order becomes the
current one and the same tick continues there (see *Idle, advancing, stop*).

Repo `um-dia-a-gente-faz/wow-server`, board = org project 7. Use
`gh` (auth: `gh auth status`). Never run anything on the VM.

## Skills you use

| Step | Skill |
|---|---|
| Understand the issue | `superpowers:brainstorming`, bounded path. The issue text and acceptance criteria are the agreed design; do not wait for a human. If the issue is architectural or ambiguous, do not guess: comment your questions on the issue, skip it, tell the owner in the tick summary. |
| Build | `superpowers:test-driven-development` + `ponytail` (the least code that solves it: reuse, stdlib, no new abstraction) |
| Before claiming done | `superpowers:verification-before-completion` |
| Review fixes | `superpowers:receiving-code-review` |
| Commit messages, loop comments | `caveman-commit`, and terse caveman style in tick comments. PR body and issue text stay normal prose. |

## A tick, in order — stop at the first step that acts

### 1. My open PRs first

My PRs are the ones whose body contains `<!-- milestone-loop -->`:

```bash
gh pr list -R um-dia-a-gente-faz/wow-server --state open --json number,body,headRefName,mergeable,statusCheckRollup,comments,commits \
  --jq '.[] | select(.body|contains("<!-- milestone-loop -->"))'
```

For each, read its comments. A review is a comment starting `## Review by <agent>` with a
`**Verdict:** approve | request-changes | comment` line (shape: `.github/REVIEW_TEMPLATE.md`).
Only a review posted **after my latest push** counts, and I never write one myself.

- **No review yet** → nothing to do for this PR.
- **Verdict `request-changes`** → check out its branch in its worktree
  (`../wowwork-gh-<n>`), implement every item under *Required fixes* (or push back with a
  reason), commit, push, then reply with one comment answering each numbered point.
  Wait for CI green. Then continue to the merge check below in the same tick only if
  no newer review has arrived; otherwise stop the tick here.
- **Verdict `approve`, or `comment` with no required fixes** → merge check.

**Merge check — merge only if ALL hold, otherwise do not merge:**
1. The review is from another agent, newer than my last push.
2. CI is green: `gh pr checks <n>`.
3. `mergeable` is `MERGEABLE`. Merge `origin/main` into the branch, rerun the tests
   (`python3 -m unittest discover -s agent/tests`, plus `tools/chat-feed/tests` and
   `tools/wowmap/tests` if touched) and push if that changed anything; wait for CI again.
4. No unaddressed change request, and no comment saying "shelving" or "hold".
5. Template followed, `Closes #<n>` present.

Then: `gh pr merge <n> --squash --delete-branch`. **One merge per tick** — every merge is a
deploy that disconnects players. After merging: confirm the issue is closed and *Done* on the
board, remove the worktree (`git worktree remove ../wowwork-gh-<n>`), `git fetch origin`.
End the tick.

### 2. Pick an issue

Only if I have no open PR waiting on me. Eligible = all of:
- open, in the current milestone: `gh issue list -R … --milestone "<m>" --state open --json number,title,labels,assignees,body`
- board status *Todo* (`gh project item-list 7 --owner um-dia-a-gente-faz --limit 200 --format json`)
- no assignee, and no open PR already closing it
- **no open blocker**: parse the `## Blocked by` section of the body for `#<n>` and check each
  is closed; also check the native field:
  `gh api graphql -f query='query{repository(owner:"um-dia-a-gente-faz",name:"wow-server"){issue(number:N){blockedBy(first:20){nodes{number state}}}}}'`
- not an umbrella/staged issue you cannot finish in one PR (e.g. a multi-stage roster plan):
  report it instead of starting it

Take the lowest-numbered eligible one. None eligible in the current milestone → check
*Milestone done* below before anything else: if every issue in it is closed and none of my
PRs is open, the milestone is finished, so advance to the next milestone **in this same tick**
and continue from step 2 on it. Fall through to *Idle* only when a later milestone also has
nothing eligible.

### 3. Develop it

1. Board → *In progress*:
   ```bash
   P=$(gh project view 7 --owner um-dia-a-gente-faz --format json --jq .id)
   gh project field-list 7 --owner um-dia-a-gente-faz --format json   # Status field id + option ids
   gh project item-edit --project-id $P --id <item id> --field-id <status field id> --single-select-option-id <In progress id>
   ```
2. `git fetch origin && git worktree add ../wowwork-gh-<n> -b feature/gh-<n>-<slug> origin/main`
   (`bugfix/` for a bug). Base is always `main`; never stack.
3. Brainstorm (bounded), write the failing test, make it pass with the least code, run the
   suites above, `verification-before-completion`. Anything you cannot verify (live VM, the
   owner in game) stays unticked under *How to test* as a human step.
4. Commit with `caveman-commit` (Conventional Commits), push.
5. Open the PR to `main` from `.github/PULL_REQUEST_TEMPLATE.md`: title `#<n>: <summary>`,
   `Issue: #<n>`, `Closes #<n>`, and the marker line `<!-- milestone-loop -->` in the body.
   `gh pr checks <n> --watch`; when green, board → *In Review*.
6. End the tick. The review comes from another agent; do not review it yourself.

### 4. Re-arm

Call `ScheduleWakeup` with `delaySeconds: 300`, prompt `/milestone-loop <current milestone> idle=<n>`,
`noop: true` when this tick changed nothing (still waiting), `false` otherwise. Never pass
`stop: true` while any later milestone still has open issues — the loop ends only on
*All done* or an explicit owner stop. Write a two-line summary in the reply: what you did,
what you are waiting for.

## Idle, advancing, stop

- Nothing eligible but my PRs or other agents' PRs are open → wait (`noop: true`, `idle`+1).
  Any tick that acts resets `idle` to 0.
- **Milestone done** = every issue in it is closed (merged) and none of my PRs is open. Then
  the current milestone becomes the next one in order, and the same tick continues from
  step 2 on it. Say so in the tick summary: milestone name, issues merged.
- **Advancing is mandatory, not a judgement call.** Moving on when a milestone is finished is
  not optional and not something to defer to the owner. The current milestone becomes the next
  one in order and work continues in the same tick: M2 → M3 → M4 → M5 → M6 → M7 → Later. A
  milestone counts as finished as soon as its issues are closed, whether or not it was the
  milestone the loop was started at.
- **Milestone stuck** = `idle` reaches 12 (about an hour) with open issues left that I cannot
  act on (blocked by something outside my PRs, skipped as ambiguous or too big, or owned by
  another agent). Do not wait forever: record them as *left over* for the final report,
  treat the milestone as passed, move to the next one and reset `idle`.
- **All done** = no later milestone has open issues, and none of my PRs is open →
  `ScheduleWakeup` with `stop: true`, then report per milestone: issues merged, left over and
  why, human steps still open.
- Left-over issues are revisited only if the owner restarts the loop at that milestone.

## Never

- Never merge a PR that is not mine, has no review from another agent, has red CI, a
  conflict, or an open change request.
- Never touch PRs the loop did not open (they belong to other agents), even if they
  look ready.
- Never push to `main`, force-push a shared branch, restart the worldserver, run
  `scripts/deploy.sh`, or use GM commands on agent characters.
- Never print or commit credentials.
- Never widen a PR: a new problem found on the way becomes its own issue in the right
  milestone.
- If anything is ambiguous, risky or outside this list, stop the loop and ask the owner.
