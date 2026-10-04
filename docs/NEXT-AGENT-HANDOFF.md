# Agent handoff (superseded)

This file used to be the task brief for building the agent's perception layer
(parsing `SMSG_UPDATE_OBJECT`). That work is done and merged, and the layout and
field tables the old brief carried were wrong. It is kept only as a pointer.

Where the current information lives:

| Need | Read |
|---|---|
| Owner's decisions (goal, milestones, model and chat rules) | `docs/AGENT-DIRECTION.md` |
| Verified 3.3.5a wire formats | `docs/PROTOCOL-NOTES.md` |
| Services, ports, data flow | `docs/ARCHITECTURE.md` |
| Deploying and running agents on the VM | `docs/DEPLOYMENT.md`, `docs/WOW-AGENTS-PROVISIONING.md` |
| Creating and retiring agent characters | `tools/agent-runner/README.md` |
| Branches, commits, PRs, live-testing rules | `CONTRIBUTING.md` |
| Open work | Linear (`UM-*`) and the GitHub issues; `docs/ROADMAP.md` for background |

Run an agent locally against the live realm (credentials come from `.env` on the
VM, never from this repo):

```bash
export WOW_ACCOUNT=AGENT01 WOW_PASSWORD='<see .env>' WOW_CHARACTER=Luaprata
python3 -m agent --dry-run
```

The old Node.js runtime (`agent-runtime/`, GM commands over MySQL) was removed and
lives only in git history. The Python `agent/` package is the real protocol client.
