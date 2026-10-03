---
title: "<ref>: <brief summary>"
labels: []
assignees: []
---

<!-- PR title must follow: <ref>: <brief summary>
     <ref> = gh-<n> / #<n> for a GitHub-native issue, UM-<n> for one mirrored from Linear.
     Examples: "#136: Add the agent runner", "UM-123: Add auto-deploy poller" -->

Issue: <ref>

Closes <ref>
<!-- Always Closes: only a closing keyword links the PR to the issue (Refs does not).
     If live/human steps remain, keep Closes, leave them unticked under "How to test",
     and add: "> `Closes` is here so GitHub and the board link this PR to the issue.
     The live steps under *How to test* are **not done**: reopen the issue if they fail." -->

## What

<!-- What does this PR do? One or two sentences. -->

## Why

<!-- Why is this change needed? Add context beyond the linked Linear issue if useful. -->

## How to test

<!-- Step-by-step instructions to verify the change works as intended. -->
<!-- Include commands, expected output, endpoints to curl, dashboards to check. -->

1.
2.
3.

## Screenshots / logs (if UI or service change)

<!-- Paste relevant output here. -->

## Checklist

- [ ] PR title follows `<ref>: <brief summary>`
- [ ] Issue linked above (`Issue: <ref>`) and the body says `Closes <ref>` (never `Refs`; unticked live steps go under "How to test") — the board's *Linked pull requests* field reads this
- [ ] Issue is on the board: *In progress* while it was being written, *In Review* now that the PR is open
- [ ] Tested on the live VM (192.168.1.64)
- [ ] All Prometheus targets still healthy (`/api/v1/targets`)
- [ ] Grafana dashboards load without errors
- [ ] No new secrets or credentials committed
- [ ] `.gitignore` covers any generated or large files
- [ ] Docs updated if this changes a documented workflow

## Conventional commit type for the merge message

<!-- e.g. feat, fix, docs, chore, refactor, perf -->

Type: ``
Scope (optional): ``