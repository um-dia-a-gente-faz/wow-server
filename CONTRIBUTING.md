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
| `transform` | `tools/wowmap/wowmap/transform.py` (DBC → pixel math) |
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
  branch starts, *In Review* once the PR is open with CI green. *Done* is set by the author
  on merge.
- Is a single logical change.
- Has CI green (`.github/workflows/ci.yml`: py_compile, unit tests, compose
  and dashboard validation, gitleaks, agent image build) before merging.
  CI cannot reach the LAN VMs, so it does not replace live testing.
  CI is path-aware: a `changes` job skips the agent/tools/compose/docker jobs a PR
  does not touch. `agent/` changes also run `tools`, because the tools suites and the
  generated-file check use `agent/`. **`ci-ok` is the single required check**: it passes when every needed job
  passed or was skipped by the filter and fails if any failed or was cancelled. Require only
  it (never the individual jobs) so adding or skipping a job does not change the rules.
  Any change to `ci.yml`, `scripts/`, `Dockerfile` or lint config runs every job.
- Is tested on the live VM before merging.
- Includes the conventional commit type in the template for the squash-merge message.

**Where the `tools` job's time goes (#591).** The job `tools (unit tests, generated files)`
has a 10-minute timeout. It runs `scripts/check.sh generated test`, which is the
generated-file checks and then every suite in `SUITES` in sequence, then the JevClient
step against a running jev-mock (about 6 s). Job setup, checkout, `setup-python` and the two pip
installs take about 8 s before the first suite, and post-job cleanup about 2 s after. The limit is at least 2x the job's duration: the job in run 38226653245 on `main`
(2026-10-12, a push that ran every job) took 4m33s in total. Wall time per suite is the gap between
consecutive suite starts in that job's log, slowest first:

| Suite | Tests | Wall time (s) |
|---|---|---|
| agent | 1727 | 143.1 |
| wowmap | 262 | 39.0 |
| world-mock | 34 | 36.5 |
| loadtest | 14 | 19.2 |
| agent-runner | 74 | 5.8 |
| scripts | 162 | 4.0 |
| chat-feed | 51 | 3.9 |
| jev-mock | 17 | 3.7 |
| common | 32 | 0.9 |
| generated checks | - | 0.7 |
| tools-arch | 28 | 0.4 |
| wow-agents | 19 | 0.2 |
| dbc | 48 | 0.1 |
| wowmap-js | 9 | 0.1 |
| wow-exporter | 8 | 0.1 |

`agent` is most of the job. It is already past that mark (143 s in this run), and the separate `agent` job runs the same suite, so it is the first place to look; #591 only raised the limit. A suite that grows past about a minute is a reason to look at
it (what it runs, what it waits on), not a reason to raise the timeout again.

Milestone and board naming (`M<n> - <subject>`, label and view rules, *blocked by*) is
in `CLAUDE.md`, *Milestones and board naming*.

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

## Generated files

Committed files built from a source of truth (`docker-compose.agents.yml`, the
`monitoring/grafana-dashboard-*` the `scripts/gen-*` scripts produce, `docs/AGENT-API.md`)
are listed in `GENERATORS` in `scripts/generate.py`. Never hand-edit one: change the
generator or its source, then `python3 scripts/generate.py`. CI runs
`scripts/generate.py --check` (inside `scripts/check.sh generated`) and fails naming the
generator to run. A new generator is one line in `GENERATORS`; it must rewrite its outputs
and, given `--check`, exit non-zero on drift (`scripts/genlib.py` has the helper).

## Lint and types

CI job `lint + types` runs `ruff` and `mypy`. They are dev tools only (`agent/` stays
stdlib-only at runtime). Locally:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .   # rules + baseline in ruff.toml
.venv/bin/mypy           # typed allowlist in mypy.ini
```

- **Ruff rules land in stages (#386),** one small PR per stage, each with a baseline of
  the files that violate it that day. Enabled: `E9`, `F` (baseline cleared, keep it empty),
  `B` (bugbear), `E722` (bare `except`, no violations), `BLE001`, `UP`, `SIM` (#413).
  To add a stage: add the rule to `select` in `ruff.toml`, run
  `ruff check . --fix` (safe fixes only, never `--unsafe-fixes`) and review every hunk,
  then list each remaining `ruff check . --output-format concise` hit as
  `"<file>" = ["<rule>"]`.
- **`#413` decisions.** `UP038` (`isinstance` with a tuple to `X | Y`) is in `ignore`: the
  union form is slower at runtime, and ruff's own docs say so for hot paths. `C901`
  (complexity) is not enabled: the size budget below (80 lines per function, 600 per
  module, `agent/tests/test_size_budget.py`) is the complexity control, and `C901` at ruff's
  default 10 would flag 33 functions and add a second, overlapping ratchet. Revisit if the
  size budget proves too weak. `BLE001` sites are the deliberate catch-and-log sites of the
  swallowed-error policy (#306): narrowing them is behaviour work, not lint work. `SIM115`
  is fixed by hand where the handle's lifetime is one read; long-lived handles are baselined.
  A lint stage baselines, and does not edit, two groups of paths. `docker-compose.yml`,
  `tdb/*`, `tools/chat-feed/*` and `tools/common/*`: `scripts/deploy.sh` (`stacks_for`, the
  source of truth for this list) recreates the game stack, and with it the worldserver, when
  one of them changes (`tools/common/*` is copied into the chat-feed image, ADR 0014). `scripts/*` and
  `.github/*`: a PR touching them waits for the owner's go-ahead (`CLAUDE.md`, *Working model*).
- **The baseline is keyed by path.** A file you move keeps its debt: move its line with
  it. A file that is new must be clean, and gets no line.
- **An import other modules reach through this one** (`npc.CMSG_BUY_ITEM`) is a re-export,
  not an unused import: mark it `# noqa: F401` with the reason. `ruff --fix` deletes it
  otherwise, and nothing fails until the caller runs.

- **Ratchet, never loosen.** `ruff.toml` `per-file-ignores` lists files that already
  violated a rule when the gate landed; fix a file's violations and delete its line.
  New code must be clean.
- **Typing tiers (#350):** `mypy` checks all of `agent/` (tests excluded). A module is
  `strict` (`[mypy-<module>]` with `disallow_untyped_defs`), `checked` (no section, the
  default: a new `agent/*.py` lands here and must pass) or `legacy` (`[mypy-<module>]
  ignore_errors = True`, a baseline that may only shrink). To move a module up: fix its
  errors, delete its legacy section in `mypy.ini` and its line in `BASELINE` in
  `scripts/tests/test_mypy_ratchet.py`; to make it `strict`, replace the section with
  `disallow_untyped_defs = True`. Count the legacy modules in the PR description. Never
  add a module to legacy. mypy was chosen over pyright because it is pip-only (no Node
  download in CI) and its gradual mode matches the tiers.
- **Legacy error baseline (#378):** `scripts/check.sh types` (what CI runs) includes it, as does `types-legacy` on its own and `all`. It fails when a legacy module has more mypy errors than its count in `mypy-legacy-baseline.json`. The counts do not depend on installed packages: the check runs mypy with `--no-site-packages`, so a `.venv` with Pillow or PyMySQL gives the same numbers as CI.
- After fixing errors in a legacy module, run `python3 scripts/mypy_legacy_baseline.py --update` and commit the JSON. A lower count is a note, never a failure.

## Size budget

`agent/tests/test_size_budget.py` (part of `scripts/check.sh test-agent`) measures the
non-test code under `agent/` and `tools/` with `ast`:

- a **function** is at most **80 lines** (`def` line to its last line);
- a **module** is at most **600 lines**.

`BASELINE` in that file lists the functions and modules that were already over when the
gate landed, each with its size then. Same ratchet as ruff and mypy:

- **New code never gets an entry.** Over budget means split it.
- **An entry is a ceiling.** A baselined function or module that grows past its entry
  fails the test.
- **Entries only go down.** When you shrink or split a baselined offender, lower its entry
  to the new size, or delete it once it is within budget or gone. A stale entry (too high,
  within budget, or gone) fails the test and the message says to delete or lower it, so a
  PR that shrinks an offender must update `BASELINE` in the same PR. If another PR
  shrinks the same offender first, rebase and lower the entry.

## Secrets

- Never commit credentials, API keys, or tokens.
- Passwords and keys live in environment variables, `.env` files (gitignored), or Docker secrets.
- If a secret accidentally lands in a commit, rotate it immediately and force-push a cleaned history.

## Local dev stack

`dev/dev.sh up` runs the mocks, one agent, chat-feed and wowmap on a fixture DB with no
credentials; see `docs/DEV-STACK.md`. Prefer it to the live VM for anything a mock covers.

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

## Add a brain scenario after a brain bug

A live brain mistake gets a golden scenario in
[`agent/tests/test_brain_scenarios.py`](agent/tests/test_brain_scenarios.py), so the fix stays
fixed (#388, #475).

1. Find the bad cycle in the agent's audit log on the VM,
   `/opt/wow-server-metrics/audit/<agent>/<YYYY-MM-DD>.jsonl`:
   `python3 -m tools.agent_dev.replay <file> --failures` lists the invalid or failed cycles.
   Full snapshots are written only every 20th cycle and on invalid or failed cycles, so
   capture a cycle that carries one.
2. Capture it: `python3 -m tools.agent_dev.capture_scenario <file> --cycle <N> --me <handle>`
   prints a `"captured_<agent>_<N>": dict(...)` entry. The audit log keeps GUIDs as handle
   strings (`u1`, `p3`) and not the handle table, so the entry carries its own: `handles` maps
   each handle to an integer, and `my_guid` is the number of the player's own handle, given by
   `--me`. `--me` must be the PLAYER's handle, the `p<n>` that units attacking you show as
   their `target_guid`; the tool cannot tell a mob's handle from the player's, so a mob's
   handle (`u<n>`) would be accepted and give a wrong scenario. `--me` must match a handle in
   the snapshot exactly (case-sensitive) or the tool exits 2 naming it.
   If no unit targets the player, omit `--me` and set `my_guid` by hand to an integer that is
   not in `handles` (for example one more than the largest number there); `my_guid=...` is
   otherwise left for you to fill in, and the predicates are `...` until you write them.
   The action the brain chose is a comment. The agent does not write `history` to the audit
   log; add it by hand if the scenario needs it.
3. Paste the entry into `SCENARIOS` and write `offered` (what the candidate list must never
   contain) and `chosen` (what the brain must never pick). State what must never happen, not
   the one right answer. The predicate helpers at the top of the file cover the common cases.
   Refer to units by the handle string in the entry (`offering_attack_on("u1")`).
4. Run `python3 -m unittest agent.tests.test_brain_scenarios`; the new scenario must fail on
   the bug.
5. Fix the code and see it pass. Keep the scenario: it is the regression test. If the fix has
   to wait, mark it `known_gap=True` with its issue number; the test then fails as soon as the
   gap closes, so the flag comes off with the fix.
