# ADR 0018: Persisted-state schema versioning and migration

## Status

Proposed (#600, the ADR part of #396). The owner accepts it later. This ADR changes no
code: it records the inventory, the rule and the first slice. #396 stays open for the
helper and the migration tests.

## Context

State that outlives a process is written by one version of the code and read by another.
That is routine here, not an edge case: the agents run on the `wow-agents` VM from their
own checkout, fast-forwarded on its own schedule (`scripts/wow-agents/bootstrap-guest.sh`,
ADR 0002), while the wow-server VM follows `main` within five minutes (`scripts/deploy.sh`,
which does not touch the agents); a running
agent container keeps its image until it is recreated; and `tools/wowmap` is rebuilt only
when its own paths change (`stacks_for` in `scripts/deploy.sh`). ADR 0010 gave the HTTP API
a version for the same reason. Files have no stated rule.

### Inventory (read from `main`, not from #396)

| Artefact | Class | Writer | Readers | Version today | Unknown format today |
|---|---|---|---|---|---|
| `~/.cache/wow-agent/names.json` (`DEFAULT_CACHE_PATH` in `agent/names.py`) | cache | `NameCache.save` | `NameCache.load`, called from `WoWSession.login_character` in `agent/session.py` | none | Missing file or invalid JSON: cold start. Valid JSON of another shape: `[]` raises `AttributeError` and a non-numeric key raises `ValueError`, both outside the `try` in `load`, so they reach `login_character`. A record whose inner shape changed is loaded as it is |
| `~/.cache/wow-agent/items.json` (`agent/items.py`) | cache | `ItemCache.save` | nobody: `ItemCache.load` has no caller outside the tests | none | not applicable, the file is write-only |
| `AGENT_AUDIT_DIR/<agent>/<day>.jsonl[.N]` | record log | `AuditLogger.record` in `agent/audit.py`, through `AuditRecord.to_json` | `iter_records` in `agent/metrics.py` (used by `monitoring/agent_metrics_textfile.py` and by `Brain.restore_jev_spend` in `agent/brain.py`), `load_records` in `tools/agent_dev/replay.py`, `tools/agent_dev/capture_scenario.py`; and two readers that parse the line themselves: the audit poller in `tools/wowmap/wowmap/activity.py` (`audit_event`) and `Fleet._last_record` in `tools/agent-runner/fleet.py` | `schema`, `SCHEMA_VERSION = 2` in `agent/audit_schema.py` | `AuditRecord.from_json` does not look at `schema`: missing keys take defaults, unknown keys go to `extra`, and `to_json` stamps the reader's own version, so a line from a newer writer comes out labelled 2. The two raw readers use `.get` |
| `memory.db`, one per agent (ADR 0011) | SQLite, advisory | `Memory` in `agent/memory.py` | the same class | `PRAGMA user_version`, `MIGRATIONS`, `SCHEMA_VERSION` | Newer or corrupt: an empty in-memory store, one warning, the file untouched (`_open`, `_migrate`). No file exists yet: nothing constructs `Memory` and `AGENT_STATE_DIR` is not a setting |
| `agents/roster.json` | document, committed | `scripts/create_agent_roster.py` (`write_text`, not atomic) and hand edits | `load_roster` in `agents/roster.py`, through `scripts/gen_agents_compose.py` and `Fleet.roster` | none; git | `RosterError` for a missing or unknown entry key. Unknown top-level keys are ignored |
| `<runtime>/state.json` on `wow-agents` (ADR 0002, D5) | document, authoritative | `Fleet.save_agent` in `tools/agent-runner/fleet.py` | `Fleet.roster`, `Fleet.save_agent` | none | Invalid JSON raises `ValueError` from `json.loads` in both, so nothing is overwritten. Unknown top-level keys survive a save. Entries are not validated (`entry["account"]`) |
| `calibration.json` (`CALIBRATION_FILE`; in production the bind mount `/opt/wowmap-data/calibration.json`) | document, authoritative | `save_calibration` in `tools/wowmap/wowmap/calibration.py` | `load_calibrations`, at import | none | Invalid or non-object: a warning and an empty store. The next `save_calibration` then writes that empty store plus one zone over the file, so every other zone's offset is lost |
| `ACTIVITY_DB` (`/data/activity.sqlite3`) | SQLite, derived | `ActivityStore` in `tools/wowmap/wowmap/activity.py` | the same class | none (`user_version` stays 0); `SCHEMA` is `CREATE TABLE IF NOT EXISTS` | An existing file with another `events` shape is opened as it is; the first statement naming a missing column fails at run time. A file that cannot be opened disables the feed (`start_activity` in `app.py`) |
| probe report (`save_report` / `load_last_report` in `tools/agent_dev/probe.py`) | cache | the probe | the probe | `schema`, `REPORT_SCHEMA = 1`, exact match | Any other value reads as `never_probed`; the next save overwrites it |
| `refs/deployed/game`, `refs/deployed/monitoring` | exempt | `scripts/deploy.sh` | `since` in the same script | git owns the format | An absent ref means "as of HEAD" |
| `METRICS_DIR/deploy.log` | exempt | `log` in `scripts/deploy.sh` | people only; no code parses it | none | not applicable |
| `<runtime>/docker-compose.agents.yml`, the `.prom` textfile, `docs/AGENT-API.md` | exempt, generated | `Fleet.write_compose`, `monitoring/agent_metrics_textfile.py`, `agent/api_contract.py` | Docker, node-exporter, people | none | Rewritten whole on every run |

Not on the list because they do not exist: the site store (#325 is open; ADR 0012 only names
SQLite) and a runner action log.

### What #396 says that is no longer true, or never was

- **"Coming: #276, #277, #279."** All three were closed on 2026-10-10. #276 is the module
  `agent/memory.py`, not wired in. #277 is `agent/audit_schema.py`. #279 was closed as the
  module split only: its Decision comment dropped the action log, and the runner still logs
  each action as a line through `Handler._audit_write` in `tools/agent-runner/handler.py`,
  which goes to the journal, not to a file. Only #325 is still to come.
- **"Only the HTTP API has a contract."** The audit record has had one since #277, and
  the memory store already does what point 2 of the proposal asks.
- **"Coordinate with the shared stdlib lib in #263."** #263 is closed and the library is
  `tools/common`. `agent/` may not import it (`test_runtime_does_not_import_tools` in
  `agent/tests/test_architecture.py`) and the agent image copies `agent/` only
  (`Dockerfile`), so it cannot hold the helper `names.py` would use.
- **"tmp + rename, as `names.py` already does."** Seven places write that way
  (`names.py`, `items.py`, `Fleet.save_agent`, `Fleet.write_compose`, `save_calibration`,
  `save_report`, the textfile script) and none calls `fsync`, so a power loss can leave an
  empty file under the final name.
- **"A silently reset memory."** The memory store warns once and keeps the file. The real
  silent losses are the two above: `load_calibrations` followed by a save, and a name cache
  record of an older shape served as current.
- **The path** is `tools/wowmap/wowmap/calibration.json`.
- **"Each is read by a different version."** True for the audit log (written on
  `wow-agents`, pulled to the wow-server VM every 30 s, ADR 0002 D4) and for `state.json`.
  Not true yet for the two caches: the agent runs as root with no volume except the audit
  directory (`scripts/gen_agents_compose.py`), so they sit in the container's writable layer
  and a new image starts without them. It becomes true the day agent state gets a volume
  (ADR 0011, open question 1).
- **ADR 0011 says the audit log "is never read back as state".** `Brain.restore_jev_spend`
  reads it at start to restore the Jev budget (`agent/__main__.py`), so it is state.

### Three conventions already exist

`schema` in the audit line and the probe report, `api_version` in HTTP responses, and
`PRAGMA user_version` in SQLite. No file on disk uses the `schema_version` key #396
proposes, and 14 days of retained audit lines use `schema`.

## Options considered

1. **Leave it per store.** No cost now. Each new store picks its own rule; the two silent
   losses stay. Nothing to undo.
2. **#396 as written: `schema_version` in every artefact, readers accept N and N-1, one
   shared helper.** Simple to state, wrong in three places. A record log must be readable for
   its whole retention, not one version back. A cache gains nothing from reading N-1: the
   server can rebuild it. A SQLite store migrates from any version by chaining steps. It
   also renames the key the audit line already carries, which is itself a format break, and
   it needs a module both `agent/` and `tools/` can import, which does not exist before
   #347. Undo: remove the key from each writer; old readers ignore it.
3. **One rule per class of state, the existing markers kept, the helper in `agent/` first.**
   Costs a short module and a test; touches no deploy path in its first slice. Breaks
   nothing: the cache files gain one key that today's `load` ignores. Undo: revert the PR;
   files written meanwhile still load.
4. **No versions, tolerant readers only** (ignore unknown keys, default missing ones). No
   code at all, and it is what the two raw audit readers already do. It cannot express
   "this file is newer than I am, do not write over it", which is the one failure that
   destroys data. Undo: nothing to undo.
5. **A migration tool run at deploy** (a `migrate` step in `scripts/deploy.sh`, or a
   library). The agents have no deploy script, `scripts/` is an owner-gated path, and a
   library is a pip dependency `agent/` may not have. Undo: remove the step, with every
   store left at whatever version the tool reached.
6. **Put all state in a database.** `agent/` has no database access by rule, the game
   database is not ours to add tables to, and the files in question are small. Hard to undo.

## Decision

Option 3.

**The marker.** An integer. In a JSON document or record it is the top-level key `schema`;
an absent key is version 1, as ADR 0010 does for `api_version`, so every file written
before this ADR is a valid version 1. In SQLite it is `PRAGMA user_version`. The existing
`schema`, `api_version` and `user_version` keep their names.

**One rule holds for every class: a process never writes to an artefact whose version is
newer than it understands.** What it does instead depends on what the state is for:

| Class | Older than the reader | Newer than the reader |
|---|---|---|
| Cache (rebuildable: `names.json`, `items.json`, probe report) | Not migrated: ignored and replaced by the next save | Ignored, and saving is switched off for that process, with one warning |
| Record log (append-only: audit JSONL) | Every older version is read; missing keys take defaults. That is the upgrade | Read. Unknown keys are ignored. The reader counts such lines and never rewrites the file |
| Document, authoritative (`state.json`, `calibration.json`) | Upgraded in memory by pure functions, one per step. The file changes only on the next ordinary write, and the old file is first copied to `<name>.v<old>` | Fail closed: reads raise a named error and writes are refused. The file is untouched |
| SQLite, advisory (`memory.db`, `ACTIVITY_DB`) | Ordered steps in a `MIGRATIONS` tuple, each in one transaction with its version bump, as `agent/memory.py` does today | Empty in-memory store, one warning, the file untouched |
| SQLite, authoritative (the site store, #325) | The same steps, after a copy to `<path>.v<old>.bak` made with `sqlite3.Connection.backup` | Refuse to start. An empty store is not a safe fallback for users and roles |

An unreadable file is treated like a newer one: never written over. That closes the
`load_calibrations` loss.

**Record logs change by addition only.** A key is never given a new meaning or type; a
changed meaning takes a new key. Then reading a newer line by ignoring what is unknown is
always correct, and the raw readers in wowmap and the runner are already compliant.
`schema` is bumped when a key is added, so a reader can tell which defaults it filled in.

**Migrations can always be run again.** A JSON upgrade is a function of the loaded value
and touches no file. A SQLite step commits with its version or not at all, and a second
open is a no-op. A step may only add (`CREATE TABLE`, `ADD COLUMN`, `CREATE INDEX`). A
step that drops or rewrites data is outside this ADR and needs its own decision.

**The guard ships before the bump.** Code already deployed does not know this rule. So
the version of an artefact may first go above 1 only after a release in which every reader
of it carries the check. This is ADR 0010's "consumers first, producers second".

**Exempt**, each for the reason in the inventory: `agents/roster.json` (committed, so the
file and its loader change in one commit, and the loader is strict), the git refs,
`deploy.log`, and generated files.

**Where the helper lives.** A new module `persist.py` in `agent/`, stdlib only, importing
nothing else from the package. It serves the two caches now. `agent/` is not a deploy
path, so the first slice restarts nothing. `tools/` cannot import it (the wowmap and
chat-feed images are built from `tools/` and never see `agent/`, ADR 0014), so when a tool
first needs it, a copy goes to `tools/common/` under the same file name, with a test in
`tools/tests/test_architecture.py` that fails unless the two files are byte-identical. ADR 0014 rejected committed copies because they
drift; this one cannot, and CI runs from a checkout where both exist, which is how ADR 0010
pins `API_VERSION`. `tools/common/` is a deploy path for both stacks, so that PR waits for
the owner. When #347 gives the repo one import root, the copy and its test are deleted.

Why the others lose: option 1 keeps the losses; option 2 breaks the audit key and applies
one rule to four different things; option 4 cannot protect a newer file; options 5 and 6
need a deploy step, a dependency or database access that the rules forbid.

## Consequences

- An agent rolled back to an older image runs with a cold cache and does not save, instead
  of writing an old shape over a new file. That is today's behaviour after any recreate.
- A runner or site older than its store stops with a clear error. Someone has to update
  the checkout or restore the `.v<old>` copy. That is the price of not guessing.
- The first format change of any artefact takes two releases: guard, then bump.
- A volume for agent state (ADR 0011, open question 1) must not be mounted before the first
  slice is deployed, because that is when the caches start to outlive their image.
- Two copies of a 50-line module exist for a while, held equal by a test.
- `AuditRecord.from_json` and the two raw audit readers stay as they are until slice 3;
  under the additive rule they are already correct, only blind to newer lines.

## First slice (ready issue outline)

**Title:** `feat(agent): persist.py, atomic versioned JSON for the name and item caches`

**Files**

- New, in `agent/`: `persist.py`. New, in `agent/tests/`: `test_persist.py`,
  `test_items_cache.py`
- `agent/names.py`, `agent/items.py`, `agent/tests/test_names.py`
- `agent/tests/test_architecture.py` (layer table: `persist` imports nothing; `names` and
  `items` may import it), `docs/ARCHITECTURE.md` (module map row)
- No compose file, no `scripts/`, no `tools/`.

**Exported names** (the new `persist.py`)

- `SCHEMA_KEY = "schema"`
- `write_atomic(path: str, text: str) -> None`: creates the directory, writes a temporary
  file in it with a name unique per process and thread, `flush` and `os.fsync`, then
  `os.replace`. On failure it removes the temporary file and raises `OSError`.
- `Loaded(status: str, version: int, data: dict)`, a `NamedTuple`. `status` is one of
  `MISSING`, `UNREADABLE`, `OLDER`, `OK`, `NEWER`.
- `read_versioned(path: str, current: int) -> Loaded`: never raises. `UNREADABLE` covers an
  I/O error other than a missing file, invalid JSON, a top level that is not an object, and
  a `schema` that is not an integer of at least 1 (a bool is not an integer here). An
  absent `schema` is version 1. `data` is the object without the key, and is empty unless
  the status is `OK` or `OLDER`.
- `write_versioned(path: str, current: int, body: dict) -> None`: `write_atomic` of
  `{"schema": current, **body}`.

**Behaviour** (`NameCache`, `ItemCache`)

- `CACHE_SCHEMA = 1` in each module. `save` writes through `write_versioned`.
- `load` uses `read_versioned`. `OK`: load the entries, with the shape conversion inside a
  `try`, so a wrong shape is a cold start and not an exception. `OLDER`, `MISSING`: cold
  start. `NEWER`, `UNREADABLE`: cold start, saving off for this instance, one warning that
  names the path and the version found.
- `save` still swallows `OSError`. `ItemCache.load` gains no caller in this slice.

**Done when**

- `write_atomic` leaves the old content and no temporary file when the write fails, and
  calls `os.fsync` before `os.replace`:
  `python3 -m unittest agent.tests.test_persist -v`.
- `read_versioned` returns each of the five statuses for a fixture of that kind, and
  version 1 for a file without the key: same command.
- A cache file written by `main` today (no `schema`) loads its entries:
  `python3 -m unittest agent.tests.test_names agent.tests.test_items_cache -v`.
- A file with `schema` 2 loads nothing and is byte-identical after a `save()`; the files
  `[]` and `{"creatures": {"abc": 1}}` load nothing and raise nothing: same command.
- The saved file has `"schema": 1`, and `NameCache.load` as it is on `main` reads that
  file (the test keeps a copy of the old three-line loader): same command.
- `python3 -m unittest agent.tests.test_architecture -v` passes with `persist` in the table.
- `scripts/check.sh all` passes.

**Further slices**, one issue each:

2. Fitness test: an AST scan of `agent/`, `tools/`, `scripts/` and `monitoring/` for
   `os.replace`, `open` in a write mode, `write_text` and `sqlite3.connect`, compared with
   a table of module, artefact, class and version constant or exemption (the inventory).
3. Audit readers: `iter_records` keeps the stored `schema` and counts lines newer than
   `SCHEMA_VERSION`; a test pins the keys `audit_event` and `Fleet.audit` read to
   `AuditRecord`'s fields.
4. The pinned copy of `persist.py` in `tools/common/`, and `state.json`: `schema`, entry
   validation, fail closed on newer. Deploy path, owner's go-ahead.
5. `calibration.json`: never save over an unreadable file; `schema`.
6. `ActivityStore`: `user_version` and `MIGRATIONS`, step 1 being today's `SCHEMA`.
7. A fixture file per released version for each SQLite store, migrated in a test (the
   harness #396 asks for); #325 is born with it.
8. After #347: delete the copy in `tools/common` and its test.

## Open questions for the owner

1. **Key name.** #396 says `schema_version`; the audit line and the probe report already
   use `schema`. Default: `schema`, and nothing existing is renamed.
2. **A runner older than `state.json`.** Default: fail closed, so the fleet panel shows an
   error until the checkout is updated. The alternative, falling back to the committed
   roster, would show retired agents as startable.
3. **The copy in `tools/common`.** It redeploys both stacks once (`deploy.sh` defers the
   game stack while anyone is online). Default: accept that in slice 4. The alternative is
   to wait for #347 and leave `state.json` and `calibration.json` unguarded until then.
4. **`agents/roster.json` exempt.** Default: yes. `state.json` holds roster-shaped entries
   and is versioned.
5. **Backup copies** (`<name>.v<old>`, `<path>.v<old>.bak`). Default: one per version,
   never pruned by code. They are small and are the undo.
6. **Agent state volume.** Default: only after slice 1 is live on `wow-agents`, and the
   caches move under `AGENT_STATE_DIR` in the same change that adds the setting.
