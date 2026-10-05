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

### Findings
1. `path/file.py:123` — what is wrong and why it matters. (Most important first. Say what you did not review.)

### Required fixes
1. `path/file.py:123` — what to change. When the exact replacement is known,
   give it as a suggestion block:

```suggestion
<the exact replacement lines>
```
````

Keep it short — the verdict, what is wrong, and what to change. No preamble and
no checklist dump: the comment is read by a fixing agent and by a human
skimming the PR.

## Rules

- **A verdict alone is not a review.** The verdict must follow from what was
  actually checked: the full diff and the surrounding code, the tests run and
  their result, and the linked issue's acceptance criteria. Never `approve`
  code you did not read; when something material is unverified, say so in
  Findings and use `comment` or `request-changes`.
- **The repo standards are still checked, silently.** Conventional Commits;
  branch `feature/<ref>-<slug>` or `bugfix/<ref>-<slug>` based on `main`, not
  stacked; PR title `<ref>: ...` with `Issue:` and `Closes` in the body; a
  single logical change scoped to the linked issue; tests pass locally and new
  behaviour is covered; no secrets in the diff; no generated or large files
  (`.gitignore` covers them); `agent/` stays stdlib-only and `tools/` uses only
  `pymysql`/`Pillow`; no `main` pushes, force-pushes, worldserver restarts or GM
  commands; docs updated when a documented workflow changed. A failure is a
  **Findings** entry — and, when it blocks, a **Required fixes** entry — not a
  table row. CI is not visible from the reviewer host: report it as unverifiable
  rather than passing, and the monitor gates it.
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
