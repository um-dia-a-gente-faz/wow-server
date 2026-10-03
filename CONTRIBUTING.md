# Contributing

This repo is worked on by multiple agents (Claude, Codex, Hermes). These rules
keep the output consistent regardless of who writes the code.

Coding agents: start with [`CLAUDE.md`](CLAUDE.md) (also readable as `AGENTS.md`) — the
short version of these rules plus the hard "never do this" list — and use the skills in
[`.claude/skills/`](.claude/skills/) for the recurring workflows:
`live-agent-test` (log an agent in and test on the live server), `trinity-protocol`
(3.3.5a wire formats), `pr-workflow` (ship a change), `wowmap-dev` (the observability
site).

## Conventional commits

Every commit MUST follow the [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <description>
```

### Types

| Type | When to use |
|---|---|
| `feat` | New feature, endpoint, dashboard, metric. A user-visible change. |
| `fix` | Bug fix. Include what was broken and how the fix works. |
| `docs` | Documentation only (README, ROADMAP, code comments, ADRs). |
| `chore` | Maintenance, tooling, dependency bumps, `.gitignore`, script organisation. |
| `refactor` | Code change that neither fixes a bug nor adds a feature. |
| `perf` | Performance improvement. |
| `test` | Adding or updating tests. |
| `style` | Formatting, whitespace, lint fixes — no logic change. |
| `ci` | CI/CD pipeline changes (GitHub Actions, build scripts). |

### Scope

Optional, lowercased, dash-separated. Examples:

| Scope | Covers |
|---|---|
| `exporter` | `exporters/wow-exporter/` |
| `wowmap` | `tools/wowmap/` (the live map service) |
| `dashboard` | Grafana dashboard JSONs |
| `monitoring` | `monitoring/` compose, Prometheus config |
| `docker` | `docker-compose.yml` or container changes |
| `agent` | `agent/` (Python protocol-level AI agent) |
| `transform` | `tools/wowmap/transform.py` (DBC → pixel math) |
| `docs` | any `.md` file |

### Examples

```
feat(exporter): add player position metrics (x,y,z per online character)
fix(wowmap): json-serialize dict responses in _send()
docs(roadmap): add character-inspect design notes
chore(gitignore): add artifs/ and node_modules/
refactor(wowmap): extract DbcTables into its own module
```

## PRs

Pull requests use the template at `.github/PULL_REQUEST_TEMPLATE.md`. Every PR:

- Has a title of the form `<ref>: <brief summary>`, where `<ref>` identifies the issue
  it serves: `UM-<number>` for an issue mirrored from Linear, `gh-<number>` or
  `#<number>` for a GitHub-native one (e.g. `#136: Add the agent runner`,
  `UM-123: Add auto-deploy poller`).
- Links its issue in the body: the first line is `Issue: <ref>` and the body contains
  `Closes <ref>`, which is what makes GitHub link the PR to the issue and fill the
  project board's *Linked pull requests* field. Every issue is linked to a PR, so use
  `Closes`, never `Refs` (a `Refs` mention does not link anything). When live or
  human steps remain, keep `Closes`, leave them unticked under "How to test" and add
  `> Closes is here so GitHub and the board link this PR to the issue. The live steps
  under *How to test* are not done: reopen the issue if they fail.`
  Check the link with
  `gh api graphql -f query='query{repository(owner:"um-dia-a-gente-faz",name:"wow-server"){pullRequest(number:N){closingIssuesReferences(first:5){nodes{number}}}}}'`.
  If `gh pr edit` fails (classic-projects deprecation error), edit the body with
  `gh api -X PATCH repos/um-dia-a-gente-faz/wow-server/pulls/N -F body=@file`, then
  re-check the issue's board status: adding a closing keyword can reset it.
- Moves the issue on the board (org project *wow-server*, 7): *In progress* when the
  branch starts, *In Review* once the PR is open with CI green. *Done* is the reviewer's,
  on merge.
- Is a single logical change.
- Has CI green (`.github/workflows/ci.yml`: py_compile, unit tests, compose
  and dashboard validation, gitleaks, agent image build) before merging.
  CI cannot reach the LAN VMs, so it does not replace live testing.
- Is tested on the live VM before merging.
- Includes the conventional commit type in the template for the squash-merge message.

## Branches

- `main` — the single source of truth. Always deployable.
- Every branch MUST follow `<type>/<ref>-<slug>`, where `<type>` is `feature` or
  `bugfix` and `<ref>` is the issue the branch serves — `gh-<number>` for a
  GitHub-native issue, `UM-<number>` for one mirrored from Linear:
  - `feature/gh-<number>-<slug>` / `feature/UM-<number>-<slug>` — new functionality.
  - `bugfix/gh-<number>-<slug>` / `bugfix/UM-<number>-<slug>` — bug fixes.
  - Examples: `feature/gh-136-agent-runner`, `bugfix/UM-48-chat-log-level`.
- **Every PR's base is `main`. Never stack a PR on another branch, even a
  genuinely dependent one.** Branch from `main` (or from the tip of your own
  in-progress work if you're queuing several tickets before any of them
  merge — just re-target each PR to `main` once you open it), and describe
  any real dependency in the PR body instead of encoding it in the git base.
  Reason: a PR whose base is another feature branch merges *into that
  branch*, not into `main` — if the base branch itself was already merged
  into `main` earlier (a squash-merge, which severs the commit-ancestry
  link), the PR's content silently never reaches `main` even though GitHub
  and Linear both show it as "merged." This happened for real on 2026-09-17
  (UM-38 and UM-39 were both "merged" into their stacked parent branches;
  `main` was missing both for hours before anyone noticed) — see
  `docs/AGENT-DIRECTION.md`'s "Known findings" for the recovery.
- Use git worktrees for parallel work (each worktree is its own directory):
  ```bash
  git worktree add ../wowwork-<name> -b feature/UM-<number>-<slug>
  ```

## Code style

- Python: 3.12+, follow PEP 8. No external framework — stdlib `http.server`, `urllib`, `pymysql`.
- Shell: POSIX `/bin/sh` or `bash`. No bashisms unless needed.
- SQL in Python: triple-quoted raw strings. No ORM.
- Prometheus: exporter metrics use a custom `Collector`, not module-level `Gauge`. This keeps series from going stale when a label value disappears.
- Grafana dashboards: raw JSON (no `{dashboard:..., overwrite:...}` wrapper), file-provisioned. Validate every panel expression against Prometheus before committing.

## Secrets

- Never commit credentials, API keys, or tokens.
- Passwords and keys live in environment variables, `.env` files (gitignored), or Docker secrets.
- If a secret accidentally lands in a commit, rotate it immediately and force-push a cleaned history.

## Testing on the live VM

Most changes affect the running services on `wow-server` (192.168.1.64) or `docker-stack` (192.168.1.60).

The VM's `/opt/wow-server` is a git checkout of this repo, and both the game
stack and the monitoring stack (`wow-exporter`, `wowmap`, …) run out of it.
Deploys ship whatever is on `origin/main` via `scripts/deploy.sh` — never copy
files onto the VM by hand. See `docs/DEPLOYMENT.md` → *Updating*.

```bash
# Deploy (fast-forwards /opt/wow-server to origin/main, rebuilds changed containers)
ssh root@192.168.1.64 '/opt/wow-server/scripts/deploy.sh'

# Verify
ssh root@192.168.1.64 'cd /opt/wow-server && docker compose ps && docker compose -f monitoring/docker-compose.yml ps'
ssh root@192.168.1.64 'curl -s localhost:9400/healthz'
ssh root@192.168.1.60 'curl -s http://localhost:9091/api/v1/targets | grep -c "\\"health\\":\\"up\\""'
```

The AI agent (`agent/`) is not part of the deployed stacks; test it from any
machine that can reach the server:

```bash
python3 -m agent --list-chars
docker build -t wow-agent .
```
