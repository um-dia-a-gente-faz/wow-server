# ADR 0019: One import root: the checkout, directories that are importable names, and the end of `sys.path` edits

## Status

Proposed (#753, the ADR part of #347). The owner accepts it later. This ADR changes no code:
it records the inventory, the rule and the first slice. #347 stays open for the slices.

Every statement about the code was read on `main` at `ffccd08` (the commit this PR is based
on). Counts come from the commands quoted next to them. Three things are not in the repo and
are listed as open questions instead of being asserted: what is installed on `wow-agents`,
whether the `agent-runner` and `fleet-limits` units exist there and under which path, and
whether anything outside the repo (cron, a shell alias) runs a script by path.

## Context

### How code finds other code today

There is no `pyproject.toml`, `setup.py` or `setup.cfg` (`git ls-files` finds none), so nothing
is installed and every import resolves through `sys.path`. `tools/tests/test_architecture.py`
already records the result as its `ROOTS` table, "sys.path roots, as each entry point or
importer sets them". That table is the current import model:

| Package | Roots it is imported with | Who sets them |
|---|---|---|
| `agent-runner` | `tools/agent-runner`, `tools`, `.` | `characters.py` (`tools/` first, the checkout root appended), `fleet.py` (the root appended) |
| `chat-feed` | `tools/chat-feed`, `tools` | `app.py` appends `tools/` |
| `common`, `dbc` | the package dir, `tools` | the importer, by `tools/` |
| `loadtest` | `tools/loadtest`, `tools/world-mock` | `soak.py` |
| `wowmap` | `tools/wowmap/wowmap`, `tools` | `state.py` appends `tools/`; every module imports its siblings flat (`import state`) |
| `wowmap/pipeline` | `tools/wowmap/pipeline`, `tools/wowmap/wowmap` | each `extract_*.py` inserts `../wowmap` |
| `worldknowledge` | `tools/worldknowledge`, `tools` | `build.py` appends `tools/` and the checkout root |

So there are three kinds of root in use: the checkout root (`agent`, `agents`,
`tools.agent_dev`, `tools.<name>` as namespace packages), `tools/` (`common`, `dbc`), and the
tool's own directory (flat imports of its siblings). Only `agent/`, `tools/agent_dev/`,
`tools/common/` and `tools/dbc/` have an `__init__.py` (`ls tools/*/__init__.py agent/__init__.py`);
`tools/`, `agents/` and `scripts/` have none and work as namespace packages when the checkout
root is on `sys.path`.

How the root gets there in each context:

- `scripts/check.sh` runs every suite from the checkout root through
  `python3 scripts/early_failures.py discover -s <dir>`. `early_failures.main` sets
  `sys.path[0] = os.getcwd()` ("what `python -m unittest` has: the repo root, not scripts/").
  So the root is first on `sys.path` in every suite. The `agent` suite also passes `-t .`.
- CI runs the same commands (`scripts/check.sh compile test-stdlib`, `generated test-tools`,
  `lint`, `types`). The `agent` job "installs nothing" (comment at `.github/workflows/ci.yml`
  line 71): a pip install there would break its purpose, which is to fail on a third-party
  import. The `tools` job installs `tools/wowmap/requirements.txt` and
  `exporters/wow-exporter/requirements.txt`; `lint-typing` and `coverage` install
  `requirements-dev.txt`. All with `--require-hashes`.
- `mypy.ini` has `files = agent, tools/common, tools/dbc, tools/wowmap, tools/loadtest` and
  `mypy_path = tools/wowmap/wowmap:tools/wowmap/pipeline:tools:.`, the same roots again. The
  other tools are not type-checked. `ruff.toml` selects `E9, F, B, E722, BLE001, UP, SIM`, none
  of which resolves an import, so ruff does not care about the layout.

### Inventory of `sys.path` edits

```
$ git grep -nE 'sys\.path(\.insert|\.append|\.pop|\[:0\]|\[0\] *=)' -- '*.py' ':!*/tests/*'   # 24 lines, 21 files
$ git grep -nE 'sys\.path(\.insert|\.append|\.pop|\[:0\]|\[0\] *=)' -- '*/tests/*'             # 79 lines, 62 files
$ git grep -n 'sys.path' -- '*.py' | wc -l                                                      # 132 (comments and strings too)
```

Production, 24 lines in 21 files:

| Where | Lines | What it reaches |
|---|---|---|
| `scripts/create_agent_roster.py` (2), `gen_agents_compose.py` (2: insert, then pop), `gen_agent_api_urls.py`, `jev_probe.py`, `llm_bench/benchmark.py`, `early_failures.py` (the `sys.path[0] = os.getcwd()` above, which is deliberate) | 8 | `agents/` and `agent/` from the root; sibling flat modules (`genlib`, `compose_lib`) from `scripts/` |
| `monitoring/agent_metrics_textfile.py` | 1 | `agent` |
| `tools/agent-runner/characters.py` (2), `fleet.py` | 3 | `tools/` for `common`, the root for `agents` |
| `tools/chat-feed/app.py` | 1 | `tools/` for `common` |
| `tools/loadtest/soak.py` | 1 | the root and `world-mock/` (a hyphenated directory, imported as `server`) |
| `tools/world-mock/server.py`, `death_scenario.py` | 2 | the root for `agent` |
| `tools/worldknowledge/build.py` | 1 | `tools/` and the root, for `dbc` and `agent.worldknowledge` |
| `tools/wowmap/wowmap/state.py` | 1 | `tools/`, for `common` and `dbc` |
| `tools/wowmap/pipeline/extract_icons.py`, `extract_maps.py`, `extract_models.py`, `overlay_debug.py`, `scatter_debug.py` | 5 | `../wowmap`, the runtime modules |
| `docs/spikes/map-engine-spike/build_art.py` | 1 | `wowmap/` and `pipeline/` (a spike, not run by CI) |

Tests, 79 lines in 62 files, by directory (`git grep -cE ... -- '*/tests/*'`, counted per
file): `tools/wowmap/tests` 21, `scripts/tests` 9, `tools/world-mock/tests` 6, `agent/tests` 5,
`tools/agent-runner/tests` 3, `tools/chat-feed/tests` 3, `tools/common/tests` 3,
`tools/dbc/tests` 3, `tools/tests` 3, `exporters/wow-exporter/tests` 2,
`tools/jev-mock/tests` 2, `tools/fleet-limits/tests` 1, `tools/worldknowledge/tests` 1.
Three kinds of edit, from the inserted expression:

1. **The tool's own directory, for flat imports** (`.../ "wowmap"` appears 20 times;
   `parents[1]` 9; `HERE.parent`, `parent` and `pipeline` more). These exist because the
   directory is not a package (wowmap, and the five hyphenated tools).
2. **`scripts/`** (`ROOT / "scripts"` 6, `REPO / "scripts"` 4, `repo / "scripts"` 1), for tests
   that import a script as a module (`agent/tests/test_roster.py`, `test_compose_lib.py`, ...).
3. **The checkout root** (`ROOT` 11, `REPO` 3, `parents[2]` 4, `parents[3]` 1). Some of these
   `ROOT` names are the tool's own directory, not the root: `tools/world-mock/tests/test_faults.py`
   has `ROOT = parents[1]`, which is `tools/world-mock`, and imports `server` flat. Only those
   whose `ROOT` is the checkout are kind 3.

A kind 3 edit is redundant whenever the test is started from the root, because the root is
already first on `sys.path` (see above). Measured, not assumed: with the one line removed, the
file's own suite still passed for `tools/tests/test_audit_reader_keys.py` (the two-line
guarded insert), `tools/tests/test_roster_accounts.py` (line 13), `tools/agent-runner/tests/test_runner.py`
(line 24) and `tools/worldknowledge/tests/test_build.py` (line 22), each run as
`python3 scripts/early_failures.py discover -s <its tests dir> -p <file>`; the files were
restored afterwards. `tools/jev-mock/tests/test_external.py` also passed but skipped its
tests (no `JEV_MOCK_URL`), so that run proves nothing and it stays out of the first slice.
A subprocess started with `python -I` (`agent/tests/test_death_registration.py`) needs its
own insert: `-I` drops the working directory. That one is legitimate.

### Tool directories whose name is not importable

`ls tools`: `agent-runner`, `chat-feed`, `fleet-limits`, `jev-mock`, `world-mock` have a hyphen.
`agent_dev`, `common`, `dbc`, `loadtest`, `worldknowledge`, `wowmap` are valid names.
`tools/wowmap/wowmap` has no `__init__.py` and its modules import each other flat; it is the
one layout that is a root on its own. The hyphenated five cannot be imported by name at all,
so the tests and neighbours that need them edit `sys.path` (kind 1 above), and the repo
references them by path in 80 tracked files (`git grep -l -E 'tools/(agent-runner|world-mock|jev-mock|chat-feed|fleet-limits)'`:
39 `.py`, 30 `.md`, and 11 others: `.env.example`, `.github/workflows/ci.yml`,
`agent/api_schema.json`, `dev/docker-compose.yml`, `docker-compose.agents.yml`,
`docker-compose.yml`, `monitoring/docker-compose.yml`, `ruff.toml`, `scripts/check.sh`,
`scripts/deploy.sh`, `tools/wowmap/wowmap/static/chat.js`).

### Images

| Image | Dockerfile | Context | What it copies | Entry |
|---|---|---|---|---|
| agent | `Dockerfile` | repo root (`.dockerignore` is written for it) | `COPY agent/ ./agent/` | `ENTRYPOINT ["python3", "-m", "agent"]` |
| chat-feed | `tools/chat-feed/Dockerfile` | `./tools` (`docker-compose.yml`, `dockerfile: chat-feed/Dockerfile`) | `COPY common/*.py ./common/`, `COPY chat-feed/app.py .` | `python -u app.py`; `RUN python -c "import app"` |
| wowmap | `tools/wowmap/Dockerfile` | `../tools` (`monitoring/docker-compose.yml`) | `COPY common/*.py ./common/`, `COPY dbc/__init__.py dbc/wdbc.py dbc/names.py dbc/spelltext.py ./dbc/`, `COPY wowmap/wowmap/ ./` | `python -u app.py`; `RUN python -c "import app, routes"` |
| wow-exporter | `exporters/wow-exporter/Dockerfile` | its own directory | `exporter.py backups.py deploys.py` flat; imports nothing from `tools/` | `python -u exporter.py` |

So an image is not a subset of the checkout at the same paths: chat-feed and wowmap flatten
`tools/<x>` to `/app/<x>` and the wowmap runtime to `/app`. ADR 0014 chose that on purpose
(one `sys.path` line works in a checkout and in an image). The agent image is the exception:
`/app/agent` is `agent/`, so `python3 -m agent` runs the same way in the image and in the
checkout. #347's "three Dockerfiles" is four today, and the fourth does not matter here.

### Places that run code by path

| Where | Command | Host that owns it |
|---|---|---|
| `tools/agent-runner/README.md` | `ExecStart=/usr/bin/python3 /opt/wow-server/tools/agent-runner/runner.py` | `wow-agents`, a unit nothing in the repo installs |
| `tools/fleet-limits/README.md` | `ExecStart=/usr/bin/python3 /opt/wow-server/tools/fleet-limits/fleet_limits.py` ("Text only; nothing installs it") | `wow-agents`, if anyone installed it |
| `dev/docker-compose.yml` | `python3 tools/world-mock/server.py`, `python3 tools/jev-mock/server.py`, `python3 -u tools/chat-feed/app.py`, `python3 -m agent` | a developer machine |
| `.github/workflows/ci.yml` | `python tools/jev-mock/server.py &`, `python -m unittest -v tools/jev-mock/tests/test_external.py`; path filters `^tools/world-mock/` (line 47) and `^tools/(wowmap\|dbc\|common\|chat-feed)/` (line 51); `file: tools/chat-feed/Dockerfile` | GitHub |
| `scripts/check.sh` | 16 `SUITES` lines, each `-s <dir>/tests`, including `-s tools/chat-feed/tests` and `-s tools/agent-runner/tests` | everyone |
| `monitoring/README-agent-metrics.md` | cron `*/2 * * * * cd /opt/wow-server && python3 monitoring/agent_metrics_textfile.py ...` | a host the repo does not control |
| `docs/DEPLOYMENT.md`, `docs/WOW-AGENTS-PROVISIONING.md`, `.env.example` | `python3 scripts/create_agent_roster.py`, `gen_agents_compose.py`, `gen_agent_api_urls.py`, `wow_console.py`, `python3 tools/worldknowledge/build.py ...` | whoever follows them |
| `scripts/deploy.sh` `stacks_for` | the case pattern `tools/chat-feed/*` | the wow-server VM, on every merge |

Module form (`python3 -m ...`) is already used for `agent` and for `tools.agent_dev`
(`CONTRIBUTING.md`: `python3 -m tools.agent_dev.replay`), and `tools/loadtest/__main__.py`
exists. The `agent` package never imports `agents/` or `tools/`
(`git grep -E '^(from|import) (agents|tools)' -- agent ':!agent/tests'` is empty;
`test_runtime_does_not_import_tools` in `agent/tests/test_architecture.py` enforces the second).

### Where the code runs, and what is known

- **`wow-agents`** (ADR 0002): `scripts/wow-agents/bootstrap-guest.sh` installs
  `ca-certificates curl git rsync openssh-server qemu-guest-agent` and Docker, then clones or
  fast-forwards `/opt/wow-server`. It installs no Python package and no unit. The runner runs
  from that checkout with `/usr/bin/python3`. A checkout moves only when someone runs the
  script again (ADR 0018, Context). Whether `pip` or `venv` exist there is not in the repo.
- **This machine** (the one the author and milestone loop run on): `python3 -m pip` fails with
  "No module named pip", Python is 3.14.4 (the repo targets 3.12), `ruff` and `mypy` are in
  `~/.local/bin`. So nothing here can run `pip install -e .`, and nothing in this ADR claims
  that it works.
- **No Docker on this machine**: no image build is claimed. Each image change below names the
  command someone with Docker must run.
- **`requirements-dev.txt`** (#737) is hash-locked and lists `ruff`, `mypy` and `coverage` with
  their dependencies. `setuptools` is not in it. A `pyproject.toml` with a `[build-system]`
  would make `pip install -e .` fetch a build backend that is not in any lock, or need
  `--no-build-isolation` and a locked `setuptools`; either is a change to a file #737 just
  finished.

### What #347 says that is no longer true, or never was

- **"6+ call sites"** with line numbers. There are 24 production lines in 21 files today, and
  `tools/world-mock/server.py:49` is `:59` now.
- **"wowmap's flat imports only resolve when run from its own directory."** They resolve from
  any directory when the script is run by path (the script's directory is `sys.path[0]`). What
  is true is that they are flat names, so mypy needs `mypy_path` and the image needs the layout
  `COPY wowmap/wowmap/ ./`.
- **"ruff/mypy can't resolve imports uniformly."** ruff resolves none and needs nothing; mypy
  has a `mypy_path` that lists the roots and checks five trees.
- **"#275 fitness tests have to guess the package graph."** They read it from source with
  `ast` and from the `ROOTS` table (`tools/tests/test_architecture.py`, #387). The table is
  the model this ADR starts from.
- **"Do #263 first."** Done: `tools/common` exists (ADR 0014).
- **"`pip install -e .` in CI and dev."** CI's `agent` job installs nothing by design; this
  machine has no pip; see the constraints above.
- **"Images `COPY` the package instead of ad-hoc subdirectories."** The agent image already
  copies one package. The two tool images copy subdirectories of a `tools/` context; making
  them copy "the package" means the layout change in this ADR, not an install.
- **"Add an `__init__.py` to `tools`."** Not needed: `tools` is a namespace package from the root
  (`tools/tests/test_roster_accounts.py` already imports `common` through `tools/` and
  `agents` through the root). Adding one changes nothing a test can see; the hyphens are the
  actual obstacle.
- **"Rename `world-mock`/`jev-mock`/`agent-runner`/`chat-feed` ... or add thin `__main__`
  shims."** The list leaves out `fleet-limits` (added later, #745). A shim
  needs the package to be importable, which is the thing missing.
- **"Acceptance: `grep ... sys.path.insert` returns nothing outside tests."** The count of
  tests is 79 lines, and 8 of the 24 production lines are not `insert` (five `append`, one `pop`, one `[:0]`, one `[0] =`).

### What ADR 0018 says that does not follow

ADR 0018 (Decision, "Where the helper lives") says that when #347 gives the repo one import
root, the pinned copy of `persist.py` in `tools/common/` "and its test are deleted". It does
not follow from a checkout root alone. The wowmap and chat-feed images are built from a
`tools/` context and never see `agent/`. They would see `agent/persist.py` only if the image
copied it, which needs the build context to be the repository root (the `.dockerignore` there
is written for the agent image, ADR 0014's reason to refuse it) or a second context. Slice 7
below keeps the copy unless the owner picks one of those (open question 4).

## Options considered

1. **Do nothing.** Cost: none now. The next tool copies one of 24 production hacks; the
   `ROOTS` table keeps growing a row per tool; the five hyphenated tools stay un-importable;
   the 79 test edits stay. Breaks nothing. Undo: nothing to undo.
2. **#347 as written: `pyproject.toml`, `pip install -e .` in CI and dev, delete every
   edit.** Cost: a build backend that no lock names (above); the `agent` CI job either installs
   something or keeps its edits; this machine and, as far as the repo shows, `wow-agents` have
   no pip, so the loop and the runner could not use the layout the tests assume; the images
   would still need their own `COPY` layout, because an editable install does not exist in an
   image. It does not remove the renames. What it breaks: any workflow that runs without the
   install (the author and reviewer sessions here). Undo: delete the file and put the edits
   back, which no one will have kept.
3. **The checkout root is the one import root; nothing is installed.** Every first-party
   import is `agent`, `agents`, `tools.<name>` (and `scripts.<name>` for a script another file
   imports). Tools are started as `python3 -m tools.<name>.<module>` from the root. The five
   hyphenated directories are renamed to underscores. Images mirror the checkout: the same
   dotted names at the same paths under `/app`. `pyproject.toml` is not needed. Cost: five
   renames and as many host steps (listed per slice), a changed image layout for two images, 80
   files of path references. Works on this machine, in the `agent` CI job and in the agent
   image unchanged. Undo, per slice: revert it; a renamed directory can be left behind as a
   symlink for one release (below).
4. **`PYTHONPATH` set by every launcher** (units, compose, `scripts/check.sh`, CI) instead of
   `sys.path` edits in code. No renames, no layout change. It does not touch the real problem
   (flat names inside non-packages, hyphens), only moves the line from 24 Python files to
   four launcher kinds on hosts the repo does not control, where forgetting it is a silent
   `ImportError` at the next restart. Undo: remove the variable.
5. **Thin `__main__` shims per tool** (#347 point 2, second alternative). Shim files load the
   hyphenated directory by path, which is `importlib.util.spec_from_file_location`, a dynamic
   import that `tools/tests/test_architecture.py` pins with `DYNAMIC_IMPORTS` and keeps empty
   on purpose (#391 removed the last one). Rejected for that reason. Undo: delete the shims.

## Decision

Option 3, in slices, each one valid on its own and none depending on the owner accepting the
whole.

**The rules**

1. **One import root: the checkout root.** First-party code is imported by dotted name from it:
   `agent`, `agents`, `tools.common`, `tools.dbc`, `tools.agent_runner`, and so on. A module
   never reaches a sibling by file location.
2. **No production module edits `sys.path`.** A file that today inserts a path is started as a
   module from the root instead (`python3 -m tools.agent_runner.runner`), or imports by dotted
   name. The one deliberate exception, `early_failures.py` setting `sys.path[0]` to the working
   directory so a script behaves like `python -m unittest`, stays and is named in the ratchet
   test.
3. **Tests are started from the root** (they already are: `scripts/check.sh`, CI, and the
   `python3 -m unittest ...` lines in `CONTRIBUTING.md`). A test does not edit `sys.path` to
   reach the root. It may, in a subprocess started with `-I`, because `-I` drops the working
   directory.
4. **A directory under `tools/` is a valid Python name.** `agent-runner`, `chat-feed`,
   `fleet-limits`, `jev-mock` and `world-mock` become `agent_runner`, `chat_feed`,
   `fleet_limits`, `jev_mock` and `world_mock`, one slice each. No `__init__.py` is added to
   `tools` or `agents` (namespace packages already work); each tool's own
   `__init__.py` is added when its slice needs it for `-m`.
5. **An image is a subset of the checkout at the same paths.** `chat-feed` and `wowmap` copy
   `tools/common`, `tools/dbc` and themselves into `/app/tools/...`, set `WORKDIR /app`, and
   run `python -m tools.<name>.<module>`. The build contexts stay `./tools` (ADR 0014's
   reason holds: the agent image's `.dockerignore` stays out of it), so the Dockerfile writes
   `COPY common/*.py ./tools/common/`. Imports in the code read `tools.common.env`, the same in
   a checkout and in an image. This reverses ADR 0014's flat layout, not its decision to share
   `tools/common` through the `tools/` context. The agent image is unchanged: `agent/` only.
6. **No install.** Nothing may require `pip install -e .`, a `.pth` file or a package build.
   If `pyproject.toml` is added, it carries only `[tool.*]` tables (ruff, mypy) and has neither
   `[project]` nor `[build-system]`, so `pip install .` fails by design (open question 3).
   `agent/` stays stdlib-only at run time and in the image, the hash-locked files are
   untouched, and the repo stays usable on a machine without pip.
7. **A ratchet, not a wall.** `tools/tests/` gets a test that pins the number of `sys.path`
   edit lines (production and tests counted separately) and a list of files that must have none.
   The numbers only go down; each slice lowers them in the same PR.

**Renaming a directory breaks every launcher that names the old path.** This ADR does not hide
that: each rename slice says which host step it needs. Two things make it dangerous and not
merely tedious. First, the `wow-agents` checkout moves only when someone runs
`bootstrap-guest.sh` (ADR 0018). A unit that names `/opt/wow-server/tools/agent-runner/runner.py`
keeps working until that moment and fails at the next restart after it, so the unit edit and the
checkout update must happen in one sitting, unit first. Second, `scripts/deploy.sh`'s
`stacks_for` matches `tools/chat-feed/*`: renaming the directory without editing that line
stops the game stack from deploying chat-feed changes, silently. The rename PR changes both
together, and `scripts/tests` covers `--stacks-for`. As an optional bridge a rename slice may
leave a symlink `tools/<old-name> -> <new_name>` for one release: `Path(__file__).resolve()` in
the tool follows it, so the old unit path keeps working while the host is migrated. The default
is no bridge (open question 2).

**Why the others lose.** Option 1 keeps the 24 production lines and the hyphens. Option 2
needs pip where there is none, a build backend that is in no lock, and still cannot give an
image its layout. Option 4 moves the problem to hosts the repo does not control. Option 5 is a
dynamic import the architecture test forbids.

## Consequences

- No host, no CI job and no machine needs anything installed that it does not have now.
- The `ROOTS` table in `tools/tests/test_architecture.py` shrinks to one entry (`.`) at the
  end, and its `DYNAMIC_IMPORTS` stays empty.
- `mypy.ini`'s `mypy_path` loses `tools/wowmap/wowmap:tools/wowmap/pipeline:tools` and
  `explicit_package_bases` can then resolve `tools.*` from `.`. mypy can check the other tools
  by adding them to `files`; that is a separate decision (they have no baseline).
- Running a script by path (`python3 scripts/foo.py`) stops being supported for the scripts that
  import `agents`/`agent`; docs and CI use `python3 -m scripts.foo`. `scripts/` is
  owner-gated, so that slice waits.
- A change under `tools/chat-feed/`, `tools/common/`, `tools/dbc/` or `tools/wowmap/` recreates
  a stack (`stacks_for`); the slices that must touch them are marked.
- The images' layout changes once, in two Dockerfiles, and the smoke test
  (`RUN python -c "import ..."`) in each is rewritten to the dotted name, so the CI image build
  still catches a missing file before merge.
- Between slices the repo has both forms. The ratchet test and the `ROOTS` table say which
  files are still on the old form.

## First slice (ready issue outline)

**Title:** `test: stop four tests from putting the checkout root on sys.path; pin the count of sys.path edits`

**Why this one.** It removes real edits (the ones measured above as redundant), renames no
directory, changes no image, touches no launcher and no production file. Candidates checked
against `stacks_for`: `tools/tests/`, `tools/agent-runner/`, `tools/worldknowledge/` match no
case, so merging recreates nothing. Rejected: `tools/wowmap/tests` (21 files; `tools/wowmap/*`
is the monitoring stack), `tools/chat-feed/tests` (game stack), `tools/common/tests` (both
stacks), `tools/dbc/tests` and `exporters/` (monitoring), `scripts/tests` (owner-gated path:
`scripts/`), and `agent/tests` (four of its five files reach `scripts/`, which needs the
`scripts` decision first; the fifth is the `-I` subprocess that must keep its insert).

**Files**

- `tools/tests/test_audit_reader_keys.py`: remove the `ROOT` assignment's guarded insert
  (`if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))`); `ROOT` stays if the file
  still uses it.
- `tools/tests/test_roster_accounts.py`: remove `sys.path.insert(0, str(ROOT))`; keep the
  `ROOT / "tools"` line, `common` is imported flat.
- `tools/agent-runner/tests/test_runner.py`: remove `sys.path.insert(0, str(REPO))`; keep
  `HERE.parent` (the runner's own directory).
- `tools/worldknowledge/tests/test_build.py`: remove `sys.path.insert(0, str(ROOT))`; keep the
  `tools/worldknowledge` line.
- New: `test_sys_path_edits.py` in `tools/tests`. It is stdlib-only and runs in the `tools-arch` suite.
- No `scripts/`, no `.github/`, no compose file, no Dockerfile, no production module, no
  fixture.

**Behaviour of the new test** (rule by rule)

1. It counts lines matching `sys\.path(\.insert|\.append|\.pop|\[:0\]|\[0\] *=)` in tracked
   `.py` files under `agent`, `agents`, `scripts`, `tools`, `monitoring`, `exporters`, `docs`,
   splitting a file into "test" when a path component is `tests`. It walks the tree
   (no git needed) and skips `__pycache__`.
2. `PRODUCTION_EDITS = 24` and `TEST_EDITS = 75` (79 minus the four removed; the guarded
   two-line insert is one matching line). The test fails when a count is above its constant, with
   the message "a new sys.path edit; import from the checkout root instead (ADR 0019)", and
   fails when it is below, with "lower the constant" (a ratchet that cannot be forgotten).
3. `NO_EDITS` is a set of the four files above; the test fails if any has a matching line.
4. The one deliberate exception, `scripts/early_failures.py`, is in an `ALLOWED` set that is
   subtracted from the production count and is checked to still contain a match (a stale entry
   fails).

**Done when**

| Item | Test or command |
|---|---|
| The four files have no `sys.path` edit and their suites pass from the root | `python3 scripts/early_failures.py discover -s tools/tests`, `-s tools/agent-runner/tests`, `-s tools/worldknowledge/tests` (or `scripts/check.sh test-tools-arch test-agent-runner test-worldknowledge`) |
| A new edit anywhere fails the new test | `test_sys_path_edits`: a temporary copy of the tree walk with one added line (the test exposes `count_edits(root)`), expects the over-count failure |
| The count is a ratchet in both directions | same module: a count one below the constant fails |
| The four files are checked | same module, `NO_EDITS` |
| The stale exception fails | same module, `ALLOWED` with a file that has no match fails |
| Nothing else changed | `git diff --stat` shows only the four test files and the new test; `git diff origin/main --name-only -- scripts .github docker-compose.yml tools/common tools/chat-feed tools/dbc tools/wowmap tdb monitoring exporters` is empty |
| The whole suite passes | `scripts/check.sh all` |

No host step. Nothing is deployed: none of the touched paths is in `stacks_for`.

## Further slices

One issue each, in dependency order. A slice marked "owner's go-ahead" touches a path
`CLAUDE.md` gates. "Stack" is what `stacks_for` says a merge recreates.

2. **The remaining root-only test edits that need a proof outside the suites.**
   `tools/jev-mock/tests/test_external.py` (proved by the CI step that sets `JEV_MOCK_URL`),
   `tools/jev-mock/tests/test_server.py:157`, `tools/wowmap/tests/test_agent_proxy.py:15`,
   `scripts/tests/test_action_catalog.py:11`. Stack: monitoring for the wowmap file; the rest none. Owner's
   go-ahead for the `scripts/tests` file. No host step.
3. **Rename `world-mock` to `world_mock`.** Edits: `tools/loadtest/soak.py`,
   `dev/docker-compose.yml`, `.github/workflows/ci.yml` (filter on line 47), `scripts/check.sh`
   (SUITES), `ruff.toml`, `tools/tests/test_architecture.py`, docs. Removes the six
   `tools/world-mock/tests` edits and the two production lines. Stack: none. Owner's go-ahead
   (`scripts/`, `.github/workflows/`). Host step: none; developers rerun `dev/` compose.
4. **Rename `jev-mock` to `jev_mock`.** `ci.yml` lines 116 and 122, `dev/docker-compose.yml`,
   `scripts/check.sh`, docs. Stack: none. Owner's go-ahead. Host step: none.
5. **Rename `fleet-limits` to `fleet_limits`.** README unit text, `scripts/check.sh`,
   `ruff.toml`. Stack: none. Host step on `wow-agents`: if the unit was installed, edit
   `ExecStart` to the new path (or run `python3 -m tools.fleet_limits.fleet_limits` with
   `WorkingDirectory=/opt/wow-server`), `systemctl daemon-reload`, restart it, in the same
   sitting as the checkout update. Open question 1.
6. **Rename `agent-runner` to `agent_runner`; drop its three production edits and the tests'.**
   Same host step on `wow-agents` for the runner unit, which restarts the control plane (the
   fleet page shows the runner as down until then). Whether restarting the runner stops the
   agent containers it started is not established here: `Fleet` shells out to `docker compose`
   and holds no child process, but this slice's author must read `tools/agent-runner/docker_backend.py`
   for it and say so. Stack: none. Owner's go-ahead (`scripts/check.sh`).
7. **Images mirror the checkout (rule 5).** `tools/common`, `tools/dbc`, `tools/chat-feed`,
   `tools/wowmap` Dockerfiles and imports change from `common.x` to `tools.common.x`.
   Rename `chat-feed` to `chat_feed` here, together with `docker-compose.yml`
   (`dockerfile: chat_feed/Dockerfile`), the `stacks_for` pattern and `ci.yml`. Stack: **game
   and monitoring both** (`tools/common/*` is both; `docker-compose.yml` and `tools/chat-feed/*`
   are the game stack). **Owner's go-ahead**; `deploy.sh` defers the game stack while anyone is
   online. Proof someone with Docker must run: `docker build -f tools/chat_feed/Dockerfile tools`
   and `docker build -f tools/wowmap/Dockerfile tools`; CI's `docker-build-wowmap` job does the
   same on the PR. ADR 0018's pinned copy of `persist.py` is deleted here only if open
   question 4 is answered "second context".
8. **`wowmap/wowmap` becomes a package**: relative imports, `__init__.py`, the 21 tests' and
   five pipeline scripts' edits and `state.py`'s edit gone; the pipeline is run as
   `python -m tools.wowmap.pipeline.extract_maps` (the host step: `docs/LIVE-MAP.md` shows
   `/opt/wowmap-venv/bin/python tools/wowmap/pipeline/extract_maps.py`, so whoever runs the
   pipeline by hand changes the command). Stack: monitoring. After slice 7.
9. **`scripts/` and `monitoring/` as modules.** `python3 -m scripts.gen_agents_compose` etc.;
   `docs/DEPLOYMENT.md`, `scripts/generate.py`, `ci.yml`, `check.sh`. The cron line in
   `monitoring/README-agent-metrics.md` becomes `python3 -m monitoring.agent_metrics_textfile`
   (host step on whichever host runs it: edit the crontab; the old line fails with an
   `ImportError` the first time after the checkout moves). Stack: monitoring for `monitoring/`.
   Owner's go-ahead. Removes the remaining production lines and the `scripts` test edits.
10. **Shrink the tables.** `ROOTS` in `tools/tests/test_architecture.py` to one entry,
    `mypy_path` to `.`, the ratchet constants to 0 (only `early_failures.py` left), and the
    docs map in `docs/ARCHITECTURE.md`.
11. **Optional:** `pyproject.toml` with `[tool.ruff]` and `[tool.mypy]` only (rule 6), moving
    `ruff.toml` and `mypy.ini`. Not needed by any slice above.

## Open questions for the owner

1. **Are the `agent-runner` and `fleet-limits` units installed on `wow-agents`, under which
   path, and are `pip`/`venv` present there?** Nothing in the repo says (`bootstrap-guest.sh`
   installs neither the units nor Python packages). Default: assume the runner unit exists with
   the path from its README and `fleet-limits` does not; slices 5 and 6 each print the exact
   `systemctl` steps and wait for a human, and no slice needs pip.
2. **A symlink bridge for one release after a rename.** Default: no. The host step is a
   one-line unit edit and a bridge hides a stale unit until the symlink is removed.
3. **Is a tool-config-only `pyproject.toml` wanted at all?** Default: no (slice 11 is not
   opened). `ruff.toml` and `mypy.ini` work; moving them gains nothing today.
4. **Should the tool images be able to import from `agent/`** (so ADR 0018's pinned
   `persist.py` copy can go)? Options: a repo-root build context with a `.dockerignore` for
   it, or a second named context. Default: neither; the copy and its byte-identity test stay,
   as ADR 0018 slice 4 says, and slice 7 does not depend on the answer.
5. **Direct file runs.** After the first slice `python3 tools/tests/test_audit_reader_keys.py`
   (a test file run by path from another directory) stops working for the four files, because
   the root is no longer inserted. Default: accepted; the supported forms are the
   `scripts/check.sh` suites and `python3 -m unittest` from the root.
6. **The deliberate exception in `early_failures.py`.** Default: kept (rule 2), because it
   makes a script run like `python -m unittest` and is not a way to import anything.
