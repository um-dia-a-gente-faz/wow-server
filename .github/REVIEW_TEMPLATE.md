# Review template

Shape for review comments on this repo's pull requests. The wow-agents monitor
drives the review loop: a reviewer agent (claude / codex / opencode) reads the
diff and outputs a markdown review; the monitor posts it verbatim as the PR
comment. The fixing agent — the author, or whichever agent the monitor hands
the fix round to — then answers every point in its next commit: it implements
the fix, or pushes back with a reason. Then the PR is re-reviewed.

Reviewer prompts embed this shape, so a change here must also be applied to the
wow-agents monitor's reviewer prompt (one file on the monitor host).

## The comment

````markdown
## Review by <agent>
**Verdict:** approve | request-changes | comment

### What I checked
<2-4 lines: the diff, the tests run and their result, the acceptance criteria compared against>

### Standards
| # | Standard | ✅ / ❌ / ➖ | Evidence |
|---|---|---|---|
| 1 | Commits follow Conventional Commits (`<type>(<scope>): ...`) | | |
| 2 | Branch `feature/<ref>-<slug>` or `bugfix/<ref>-<slug>`, based on `main`, not stacked | | |
| 3 | PR title `<ref>: ...`; body has `Issue:` and `Closes` | | |
| 4 | Single logical change, scoped to the linked issue | | |
| 5 | Tests pass locally (run them); new behaviour covered | | |
| 6 | CI green (not visible from the reviewer host: ➖; the monitor gates it) | | |
| 7 | No secrets or credentials in the diff | | |
| 8 | No generated or large files; `.gitignore` covers them | | |
| 9 | `agent/` stays stdlib-only; `tools/` only `pymysql`/`Pillow` | | |
| 10 | No `main` pushes, force-pushes, worldserver restarts or GM commands | | |
| 11 | Correctness: the change does what the issue asks, not just "looks right" | | |
| 12 | Docs updated when a documented workflow changed | | |

Fill every row: ✅ pass, ❌ fail, ➖ not applicable or not verifiable — with a
one-line evidence note. Never ✅ without evidence; every ❌ is explained in
Findings and listed under Required fixes if it blocks.

### Findings
1. `path/file.py:123` — what is wrong and why it matters. (Most important first. Say what you did not review.)

### Required fixes
1. `path/file.py:123` — what to change. When the exact replacement is known,
   give it as a suggestion block:

```suggestion
<the exact replacement lines>
```
````

## Rules

- **A verdict alone is not a review.** State what was actually checked (diff,
  tests, acceptance criteria) and why the verdict follows.
- **Verdicts:** `approve` — nothing blocking (minor notes still go under
  Findings); `request-changes` — at least one blocking problem, with the
  numbered fixes; `comment` — observations only, no fix round expected.
- **Suggestion blocks are exact.** The fixing agent applies them in its next
  commit (or rejects a point with a reason). Only include one when you are
  sure it is correct and complete for the cited lines — a wrong suggestion
  costs a whole round. If a fix cannot be given as exact code, describe the
  change and what "done" looks like instead.
- **Reviewers never push, edit, or merge.** The comment is the only output;
  the monitor posts it and moves the PR through the loop.