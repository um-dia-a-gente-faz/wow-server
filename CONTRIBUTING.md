# Contributing

This repo is worked on by multiple agents (Claude, Codex, Hermes). These rules
keep the output consistent regardless of who writes the code.

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

- Is a single logical change.
- Has CI green (`.github/workflows/ci.yml`: py_compile, unit tests, compose
  and dashboard validation, gitleaks, agent image build) before merging.
  CI cannot reach the LAN VMs, so it does not replace live testing.
- Is tested on the live VM before merging.
- Includes the conventional commit type in the template for the squash-merge message.
- References an issue if one exists.

## Branches

- `main` — the single source of truth. Always deployable.
- Feature branches: `feat/<slug>`, `fix/<slug>`.
- Use git worktrees for parallel work (each worktree is its own directory):
  ```bash
  git worktree add ../wowwork-<name> -b feat/<name>
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
