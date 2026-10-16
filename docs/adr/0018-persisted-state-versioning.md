# ADR 0018: Persisted-state schema versioning and migration

## Status

Proposed (#600, the ADR part of #396). The owner accepts it later. This ADR changes no
code: it records the inventory, the rule and the first slice. #396 stays open for the
helper and the migration tests.

## Context

State that outlives a process is written by one version of the code and read by another.
That is routine here, not an edge case. The agents run on the `wow-agents` VM from their
own checkout, which is fast-forwarded only when someone runs
`scripts/wow-agents/bootstrap-guest.sh` again; no timer or cron in the repo does it (the
only timer under `scripts/wow-agents/` is `wow-audit-pull.timer`, and ADR 0002 and
`tools/agent-runner/README.md` speak of an auto-update that nothing in the repo
implements). So a release reaches the agents by a manual step, while the wow-server VM
follows `main` within five minutes (`scripts/auto-deploy.sh` and `scripts/deploy.sh`,
which do not touch the agents). A running agent container keeps its image until it is
recreated; and `tools/wowmap` is rebuilt only
when its own paths change (`stacks_for` in `scripts/deploy.sh`). ADR 0010 gave the HTTP API
a version for the same reason. Files have no stated rule.

### Inventory (read from `main`, not from #396)

| Artefact | Class | Writer | Readers | Version today | Unknown format today |
|---|---|---|---|---|---|
| `~/.cache/wow-agent/names.json` (`DEFAULT_CACHE_PATH` in `agent/names.py`; or `AGENT_STATE_DIR/names.json` in an agent container, #252) | cache | `NameCache.save` | `NameCache.load`, called from `WoWSession.login_character` in `agent/session.py` | `schema`, `CACHE_SCHEMA = 2` in `agent/names.py` (#666, #671); also the additive key `cache_id` (#751), the server's `SMSG_CLIENTCACHE_VERSION`, integer or null, no schema bump | Missing or older (`schema` below 2): cold start. Newer: not read, and saving to that path is switched off. Unreadable, a wrong shape included: moved to `<name>.corrupt` with one warning (`NameCache.load`, `agent/names.py`) |
| `~/.cache/wow-agent/items.json` (`agent/items.py`; or `AGENT_STATE_DIR/items.json` in an agent container, #252) | cache | `ItemCache.save` (not called by a running agent since #712; kept for a future decision) | nobody: `ItemCache.load` has no caller outside the tests | `schema`, `CACHE_SCHEMA = 1` in `agent/items.py` | not applicable, the file is neither written nor read by a running agent |
| `AGENT_AUDIT_DIR/<agent>/<day>.jsonl[.N]` | record log | `AuditLogger.record` in `agent/audit.py`, through `AuditRecord.to_json` | `iter_records` in `agent/metrics.py` (used by `monitoring/agent_metrics_textfile.py` and by `Brain.restore_jev_spend` in `agent/brain.py`), `load_records` in `tools/agent_dev/replay.py`, `tools/agent_dev/capture_scenario.py`; and two readers that parse the line themselves: the audit poller in `tools/wowmap/wowmap/activity.py` (`audit_event`) and `Fleet._last_record` in `tools/agent-runner/fleet.py` | `schema`, `SCHEMA_VERSION = 2` in `agent/audit_schema.py` | `AuditRecord.from_json` does not look at `schema`: missing keys take defaults, unknown keys go to `extra`, and `to_json` stamps the reader's own version, so a line from a newer writer comes out labelled 2. The two raw readers use `.get` |
| `AGENT_AUDIT_DIR/<agent>/lifecycle/<day>.jsonl` (`LIFECYCLE_SUBDIR` in `agent/audit.py`) | record log | `AuditLogger.record_lifecycle` in `agent/audit.py`, which has no caller outside the tests yet | No reader is written for it: `find_audit_files` in `agent/metrics.py`, `Fleet.audit` in `tools/agent-runner/fleet.py` and the audit poller in `tools/wowmap/wowmap/activity.py` list the agent directory only and do not look into the subdirectory. One reader picks it up by accident: `tools/agent_dev/cache_probe.py`, given a directory, globs `**/*.jsonl*` recursively (`_paths`) and passes the file to `load_records` in `tools/agent_dev/replay.py` | `schema`, `LIFECYCLE_SCHEMA = 1` | In `cache_probe.py` each lifecycle line is parsed as an `AuditRecord`: it comes out labelled `schema` 2 with the default of every decision field, and `analyse` then skips it because it has no `situation_key` and no `snapshot`. Nothing checks `LIFECYCLE_SCHEMA`. Pruned with the day files by `clean_retention`; not size-rotated |
| `AGENT_AUDIT_DIR/<agent>/unmet/<day>.jsonl` (`UNMET_SUBDIR` in `agent/audit.py`, #319) | record log | `AuditLogger.record_unmet_intent` in `agent/audit.py`, through `UnmetIntents.record` in `agent/unmet.py`, which has no reader outside the tests and the `GET /unmet-intents` endpoint | `GET /unmet-intents` in `agent/http_api.py` reads the in-memory copy, not the file. No file reader is written yet. `find_audit_files` in `agent/metrics.py` lists the agent directory only. `tools/agent_dev/cache_probe.py` globs `**/*.jsonl*` recursively, so it would read these lines as `AuditRecord`s, the same hazard as the lifecycle row | `schema`, `UNMET_SCHEMA = 1` in `agent/unmet.py` | In `cache_probe.py` each line is parsed as an `AuditRecord` (see the lifecycle row). Nothing checks `UNMET_SCHEMA`. Pruned with the day files by `clean_retention`; not size-rotated |
| `<runtime>/actions.jsonl` (`AGENT_RUNNER_ACTION_LOG`, #458; empty turns it off) | record log | `Handler._action_log` in `tools/agent-runner/handler.py`, one line per start, stop, create, retire and walk, plus rejected tokens | none yet: nothing reads it back. The read endpoint for the fleet panel is a later issue | `schema`, `ACTION_LOG_SCHEMA = 1` in `tools/agent-runner/handler.py`, exact match on writing | Append-only and never rotated or pruned, so it grows without bound until an operator removes it. A line the writer cannot append is dropped with one warning per path and never fails the request. No reader rewrites the file |
| `memory.db`, one per agent (ADR 0011) | SQLite, advisory | `Memory` in `agent/memory.py` | the same class | `PRAGMA user_version`, `MIGRATIONS`, `SCHEMA_VERSION` | Newer or corrupt: an empty in-memory store, one warning, the file untouched (`_open`, `_migrate`). No file exists yet: nothing constructs `Memory` and `AGENT_STATE_DIR` is not a setting |
| `agents/roster.json` | document, committed | `save_roster` in `scripts/create_agent_roster.py` (atomic since #685 through its private `_write_atomic`: unique temporary file, fsync, `os.replace`; no validation) and hand edits | One loader: `load_roster` in `agents/roster.py`, reached through `scripts/gen_agents_compose.py`, `scripts/gen_agent_api_urls.py`, `Fleet.roster` and, since #604, `load_roster` in `scripts/create_agent_roster.py` | none; git | `RosterError` for a missing or unknown entry key; unknown top-level keys are ignored. `save_roster` rewrites the file as `{"agents": [...]}`, so any other top-level key is dropped |
| `<runtime>/state.json` on `wow-agents` (ADR 0002, D5) | document, authoritative | `Fleet.save_agent` in `tools/agent-runner/fleet.py` (atomic through the private `_write_atomic` in that file, as since #685; `schema` added in #396) | `Fleet.roster`, `Fleet.save_agent` (both through `Fleet._read_state`) | `schema`, `STATE_SCHEMA = 1` in `tools/agent-runner/fleet.py`; absent = 1. Fail closed: a newer, unreadable or malformed file, or an entry without a non-empty `account` and `character`, or `agents` not a list, is a `StateFileError` (HTTP 503 with the path, the status and the version) on every route that needs the roster, and the file is never moved or overwritten. Recovery: update the checkout, or the operator restores the file | Invalid JSON, a newer schema or a bad entry: the same 503, the file untouched. Unknown top-level keys survive a save |
| `calibration.json` (`CALIBRATION_FILE`; in production the bind mount `/opt/wowmap-data/calibration.json`) | document, authoritative | `save_calibration` in `tools/wowmap/wowmap/calibration.py` (atomic since #685 through its private `_write_atomic`) | `load_calibrations`, at import | `schema`, `CALIBRATION_SCHEMA = 1` in `tools/wowmap/wowmap/calibration.py`; read by `_read_store` (absent = 1), the key is not written while it is 1 | Unreadable (invalid JSON, not an object, a bad zone or offset, a bad `schema`) or newer: empty offsets with a warning at load, and every `save_calibration` is refused with `CalibrationUnreadable` / `CalibrationNewer`, the file untouched |
| `ACTIVITY_DB` (`/data/activity.sqlite3`) | SQLite, derived | `ActivityStore` in `tools/wowmap/wowmap/activity.py` | the same class | `PRAGMA user_version`, `MIGRATIONS` and `SCHEMA_VERSION` in `tools/wowmap/wowmap/activity_schema.py`, run by `migrate` at every open | An existing file with another `events` shape: if `character` or `t` is missing, `CREATE INDEX IF NOT EXISTS events_character_t` in `SCHEMA` fails in the constructor (`OperationalError`), and `start_activity` in `app.py` disables the feed, as it does for a file that cannot be opened. If another column is missing the store opens and the first statement naming it fails at run time |
| probe report (`save_report` / `load_last_report` in `tools/agent_dev/probe.py`) | exempt | the probe | the probe | `schema`, `REPORT_SCHEMA = 1`, exact match | Any other value reads as `never_probed`; the next save overwrites it |
| `refs/deployed/game`, `refs/deployed/monitoring` | exempt | `scripts/deploy.sh` | `since` in the same script; `scripts/auto-deploy.sh` reads `refs/deployed/game` to keep polling while the game stack is deferred | git owns the format | An absent ref means "as of HEAD" in both |
| `MODELS_DIR/<race>_<gender>.bin` (in production `/opt/wowmap-data/models`) | generated asset, binary | `tools/wowmap/pipeline/extract_models.py`, run by hand; existing files are skipped unless `--force` | the browser: `parseBlob` in `tools/wowmap/wowmap/static/charview.js`. The server only serves the bytes (`models_file` in `assets.py`); `parse_blob` in `models.py` is the Python inverse | `BLOB_MAGIC = b"WMDL"`, `BLOB_VERSION = 1` in `tools/wowmap/wowmap/models.py`, exact match | Any other magic or version: `parseBlob` throws "not a model file" and `parse_blob` raises `ValueError`. The file is never rewritten by the site |
| `<runtime>/world/world.sqlite3` (ADR 0016; the path is the setting `AGENT_WORLD_BUNDLE` (#742), and the directory is mounted read-only at `/data/world` (#749)) | generated asset, SQLite | `build` in `tools/worldknowledge/build.py` (temporary file next to `--out`, then `os.replace`), run by hand | `WorldKnowledge` in `agent/worldknowledge.py`, read-only (`mode=ro`) | `PRAGMA user_version`, `SCHEMA_VERSION = 1` in `agent/worldknowledge.py`, exact match | Another `user_version`, a missing table or column, or a corrupt file: one warning and an unavailable reader, every lookup returns None; the file is never rewritten by the reader |
| `last-success`, `last-failure` under `AUDIT_STATE_DIR` (default `/var/lib/wow-audit-pull`) | exempt | `write_state` in `scripts/wow-agents/pull-audit.sh` | `scripts/wow-agents/audit-pull-status.sh` | none | The reader takes the first word and treats anything that is not all digits as "no heartbeat" |
| `METRICS_DIR/deploy.log` | exempt | `log` in `scripts/deploy.sh` | `read_deploys` in `exporters/wow-exporter/deploys.py` (wow-exporter, at every scrape) | none | a line that does not match is skipped |
| `<runtime>/docker-compose.agents.yml`, the `.prom` textfile, `docs/AGENT-API.md` | exempt, generated | `Fleet.write_compose`, `monitoring/agent_metrics_textfile.py`, `agent/api_contract.py` | Docker, node-exporter, people | none | Rewritten whole on every run |
| `<runtime>/fleet/limits.json` (`FLEET_LIMITS_FILE`, ADR 0017) | exempt, generated | `Writer.cycle` in `tools/fleet-limits/fleet_limits.py` (temporary file next to it, fsync, `os.replace`; its private `_write_atomic`), every 15 s | `FleetLimits` in `agent/fleetlimits.py`, in each agent | `version`, `SCHEMA_VERSION = 1` in `agent/fleetlimits.py`, exact match | Any other value, a bad shape or a file over 64 KiB: no block applies and one warning. Rewritten whole every period; the blocks it computes expire in 30 s. `version` may go above 1 only after every running agent carries a reader that accepts it: an older agent fails open and the cap and the owner stop vanish |

`scripts/tests/test_persisted_state_inventory.py` enforces this table: it scans the tracked Python files for every place that writes a file or a SQLite database and fails when one has no row in its `INVENTORY` (path, function, artefact, class, version rule), when a row has no writer, and when a non-exempt artefact is in this table but not in `INVENTORY` or the other way round. A new writer needs a row here first, with its class and version rule.

Not on the list because they do not exist: the site store (#325 is open; ADR 0012 only names
SQLite).

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
- **"tmp + rename, as `names.py` already does."** Eight places write that way
  (`names.py`, `items.py`, `Fleet.save_agent`, `Fleet.write_compose`, `save_calibration`,
  `save_report`, the textfile script, and `write_state` in `pull-audit.sh` with `mv -f`) and
  none calls `fsync` or `sync`, so a power loss can leave an empty file under the final name.
- **"A silently reset memory."** The memory store warns once and keeps the file. The real
  silent losses are the two above: `load_calibrations` followed by a save, and a name cache
  record of an older shape served as current.
- **The path** is `tools/wowmap/wowmap/calibration.json`.
- **"Each is read by a different version."** True for the audit log (written on
  `wow-agents`, pulled to the wow-server VM every 30 s, ADR 0002 D4) and for `state.json`.
  For the two caches it depends on where the agent runs. In an agent container it is not
  true yet: the agent runs as root and the only volume is the audit directory (`SERVICE`
  in `agents/compose.py` adds none; it inherits the one in `x-agent` in `HEADER`), so the
  caches sit in the container's writable layer and a new image starts without them. That
  becomes true the day agent state gets a volume (ADR 0011, open question 1). On a
  developer machine it is true today: `python3 -m agent` from a checkout reads
  `~/.cache/wow-agent/` with whichever branch is checked out, and that is where the
  older-shape loss named above can already happen.
- **ADR 0011 says the audit log "is never read back as state".** `Brain.restore_jev_spend`
  reads it at start to restore the Jev budget (`agent/__main__.py`), so it is state.

### Four conventions already exist

`schema` in the audit line and the probe report, `api_version` in HTTP responses,
`PRAGMA user_version` in SQLite, and a magic plus a version integer in a binary header
(`BLOB_MAGIC`, `BLOB_VERSION`). No file on disk uses the `schema_version` key #396
proposes, and 14 days of retained audit lines use `schema`.

The model blob is the clearest case of writer and reader at different versions: the
writer is a pipeline someone runs by hand on the VM, and the reader is JavaScript that is
redeployed on every merge that touches `tools/wowmap`.

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

**The marker.** An integer. In JSON it is the key `schema`, at the top level of the
document next to the payload, or in each line of a record log. In SQLite it is
`PRAGMA user_version`; in a binary asset it is the integer after the magic. The existing
`schema`, `api_version`, `user_version` and `BLOB_VERSION` keep their names.

What an absent `schema` means depends on the artefact. In a document or a cache that is
not exempt (`state.json`, `calibration.json`, `names.json`, `items.json`) it is
version 1, as ADR 0010 does for `api_version`, so every such file written before this ADR
is a valid version 1. In the audit record it is version 0 and stays 0: the docstring of
`agent/audit_schema.py` says so, and `test_v0_record_without_schema_loads_with_defaults`
with the fixture `agent/tests/fixtures/audit/v0.jsonl` pins it.

**One rule holds for every class that is not exempt: a process never writes over state
whose version is newer than it understands.** The unit that carries the version is the file for a cache, a
document and a SQLite store, and the line for a record log. What a process does instead of
writing, and what it does with a file it cannot read at all, depends on what the state is
for. "Unreadable" means truncated, empty, of the wrong type, or with no version that parses.
Only a version that parses as an integer above the reader's own is "newer".

| Artefact | Older than the reader | Newer than the reader | Unreadable, and how to recover |
|---|---|---|---|
| Cache: `names.json`, `items.json` | Not migrated: ignored and replaced by the next save | Ignored. Saving to that path is off for the process, the file is untouched, one warning | Cold start. The file is moved to `<name>.corrupt` (one copy, replaced by the next one), one warning, and the next save writes a fresh file. No operator step: the server can rebuild a cache |
| Record log: audit JSONL, and the lifecycle JSONL under the same rule | Every older line is read; missing keys take defaults. That is the upgrade | The line is read and its unknown keys are ignored. The reader counts such lines | A line that does not parse is skipped and counted, as `iter_records` does today. No reader ever rewrites a log file |
| Document: `state.json` | Upgraded in memory by pure functions, one per step. The file changes only on the next ordinary write, and the old file is first copied to `<name>.v<old>` | Fail closed. Every request that needs the roster is answered with a named error that gives the path and the version, so nothing is started, stopped, created or retired. Recovery: update the checkout | The same refusal, with the reason. Code never writes over the file. Recovery: the operator restores it, or removes it to fall back to the committed roster. Removing it forgets the agents created at run time and un-retires the retired ones, so restoring is the first choice |
| Document: `calibration.json` | As `state.json` | wowmap starts, with empty offsets and a warning that is visible in the log and in the answer to a save. Every save is refused and the file is untouched. Recovery: deploy the newer site | The same: wowmap starts with empty offsets and refuses every save until the operator fixes or removes the file. An offsets file never stops the site. This is the behaviour #608 asks for |
| SQLite, advisory: `memory.db` | Ordered steps in a `MIGRATIONS` tuple, each in one transaction with its version bump, as `agent/memory.py` does today | An empty in-memory store for the run, one warning, the file untouched (today's behaviour) | The same, at every start, until the operator moves the file aside; the next start then creates a fresh store |
| SQLite, derived: `ACTIVITY_DB` | The same steps | The feed is disabled for the run and the file is untouched. No in-memory substitute | The same: "feed disabled" stays, as `start_activity` does today. Recovery: the operator moves the file aside and restarts wowmap |
| SQLite, authoritative: the site store (#325, not built) | The same steps, after a copy to `<path>.v<old>.bak` made with `sqlite3.Connection.backup` | Refuse to start, with a message that names the file and both versions | Refuse to start, with a clear message. Admin users and roles are never recreated silently. Recovery: the operator restores a backup |
| Binary asset: the model blob | Exact match only: the browser shows no model | The same | The same. Recovery in all three cases: run `extract_models.py --force`. A PR that changes `BLOB_VERSION` lists that run as a human step. The site never writes the file; `--force` regenerates it whole from the client files, and is the one overwrite a person asks for by name |

A `.v<old>` copy exists only after an upgrade, so it undoes an upgrade and nothing else.
It is not the recovery for a corrupt file; the last column is.

**In a record log the version belongs to the line, not to the file.** `AuditLogger`
appends without reading. After a rollback an older agent appends its lines to a day file
that already holds newer lines, rotates that file (`_rotate_if_needed` in `agent/audit.py`)
and deletes old files by retention (`clean_retention`). All three are allowed. The writer
never inspects the file and never refuses to append: refusing would lose audit lines and
the Jev budget that `Brain.restore_jev_spend` rebuilds from them. The invariant for a log
is that no process rewrites a line it did not write.

**Record logs change by addition only.** A key is never given a new meaning or type; a
changed meaning takes a new key. Then reading a newer line by ignoring what is unknown is
always correct. The raw readers depend on it: `audit_event` and `Fleet.audit` raise on a
changed type (`tool_call` as a string, `ts` as a string). `schema` is bumped when a key is
added, so a reader can tell which defaults it filled in.

This replaces the rule in the docstring of `agent/audit_schema.py`, "When a field's meaning
or shape changes, bump SCHEMA_VERSION". Under this ADR a field's meaning or shape does not
change at all. The docstring is edited in slice 3, not here.

**Migrations can always be run again.** A JSON upgrade is a function of the loaded value
and touches no file. A SQLite step commits with its version or not at all, and a second
open is a no-op. A step may only add (`CREATE TABLE`, `ADD COLUMN`, `CREATE INDEX`). A
step that drops or rewrites data is outside this ADR and needs its own decision.

**The guard ships before the bump.** Code already deployed does not know this rule. So
the version of an artefact may first go above 1 only after a release in which every reader
of it carries the check. This is ADR 0010's "consumers first, producers second".

**Exempt**, each for the reason in the inventory: `agents/roster.json` (committed, so the
file and its loader change in one commit; every reader goes through the strict
`load_roster` in `agents/roster.py`, and `save_roster` would drop a version key anyway), the git refs, `deploy.log`, generated
files, the audit-pull heartbeat (two shell scripts in one directory of one checkout,
deployed together; the content is a timestamp and the reader already rejects anything that
is not digits), and the probe report (a developer tool run by hand; `load_last_report`
wants an exact match on `REPORT_SCHEMA`, any other file, one with no `schema` included,
reads as `never_probed`, and the next probe run replaces it through `save_report`). The
marker rule above and the invariant below are for the artefacts that are not exempt.

**Where the helper lives.** A new module `persist.py` in `agent/`, stdlib only, importing
nothing else from the package. It serves the two caches now. `agent/` is not a deploy
path, so the first slice restarts nothing. `tools/` cannot import it (the wowmap and
chat-feed images are built from `tools/` and never see `agent/`, ADR 0014), so when a tool
first needs it, a copy goes to `tools/common/` under the same file name, with a test in
`tools/tests/test_architecture.py` that fails unless the two files are byte-identical.
ADR 0014 rejected committed copies because they drift; this one cannot, and CI runs from a
checkout where both exist, which is how ADR 0010 pins `API_VERSION`. `tools/common/` is a deploy path for both stacks, so that PR waits for
the owner. When #347 gives the repo one import root, the copy and its test are deleted.

Why the others lose: option 1 keeps the losses; option 2 breaks the audit key and applies
one rule to four different things; option 4 cannot protect a newer file; options 5 and 6
need a deploy step, a dependency or database access that the rules forbid.

## Consequences

- An agent rolled back to an older image runs with a cold cache and does not save, instead
  of writing an old shape over a new file. That is today's behaviour after any recreate.
- A runner older than `state.json`, or one whose `state.json` is unreadable, answers with a
  clear error until someone updates the checkout or restores the file. That is today's
  behaviour for a bad file (every `Fleet.roster` call raises), now with a name and a reason.
  wowmap never stops over `calibration.json`. That is the price of not guessing.
- A truncated or empty cache file, which a power loss can produce, heals itself on the
  next save.
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
  I/O error other than a missing file, an empty or truncated file, invalid JSON, a top
  level that is not an object, and a `schema` that is not an integer of at least 1 (a bool
  is not an integer here). `NEWER` is only an integer `schema` above `current`. An absent
  `schema` is version 1. `data` is the object without the key, and is empty unless the
  status is `OK` or `OLDER`.
- `write_versioned(path: str, current: int, body: dict) -> None`: the `schema` key sits at
  the top level next to the payload keys. A `body` that already has a `schema` key raises
  `ValueError`, so the caller's key can never replace the version. Otherwise `write_atomic`.
- `set_aside(path: str) -> None`: `os.replace(path, path + ".corrupt")`; an `OSError` is
  swallowed.
- Every function takes the path per call. The module keeps no state and does not log.

**Behaviour** (`NameCache`, `ItemCache`)

- `CACHE_SCHEMA = 1` in each module. `save` writes through `write_versioned`.
- `load` uses `read_versioned`, then by status:
  - `OK`: the load is all or nothing. Every section is converted first (`creatures` and
    `gameobjects`, or `items`) and the cache is updated only when all of them converted. A
    section that is not an object or a key that is not an integer makes the whole file
    `UNREADABLE`; nothing from it is kept.
  - `OLDER`, `MISSING`: cold start. The next save replaces the file.
  - `UNREADABLE`: cold start, `set_aside` the file, one warning. Saving stays on, so the
    next save writes a fresh file.
  - `NEWER`: cold start, one warning, the file untouched, and saving to that path is off.
- "Saving off" is per path, because `load` and `save` take an optional path per call. The
  instance keeps the set of paths it found `NEWER`. `save(p)` does nothing when `p` is in
  the set and writes normally for any other path.
- The warning is logged through the calling module's logger, `logging.getLogger(__name__)`
  in `agent/names.py` and in `agent/items.py` (new in both: neither has a logger today). It
  names the path, the status, and the version found when there is one. One warning per
  `load` call.
- `save` still swallows `OSError`. `ItemCache.load` gains no caller in this slice.

**Done when**

- `write_atomic` leaves the old content and no temporary file when the write fails, and
  calls `os.fsync` before `os.replace`:
  `python3 -m unittest agent.tests.test_persist -v`.
- `read_versioned` returns each of the five statuses for a fixture of that kind, and
  version 1 for a file without the key: same command.
- A cache file written by `main` today (no `schema`) loads its entries:
  `python3 -m unittest agent.tests.test_names agent.tests.test_items_cache -v`.
- A file with `schema` 2 loads nothing and is byte-identical after a `save()`, and a
  `save(other_path)` on the same instance still writes `other_path`: same command.
- After a `load` of each `UNREADABLE` file (zero bytes, truncated JSON, `[]`,
  `{"creatures": {"abc": 1}}`, `{"schema": "x"}`), nothing was raised, the cache is empty,
  the old bytes are in `<name>.corrupt`, and a `save()` leaves a file at the path that the
  next `load` reads as `OK`: same command.
- A file whose `creatures` section is valid and whose `gameobjects` section is not loads no
  creature either: same command.
- `load` logs exactly one warning for `NEWER` and one for `UNREADABLE`, on the logger
  `agent.names` (or `agent.items`), containing the path, and none for `OK`, `OLDER` or
  `MISSING` (`assertLogs` / `assertNoLogs`): same command.
- `write_versioned` raises `ValueError` for a `body` with a `schema` key and writes nothing:
  `python3 -m unittest agent.tests.test_persist -v`.
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
   `AuditRecord`'s fields; the docstring of `agent/audit_schema.py` states the additive-only
   rule in place of "meaning or shape changes, bump". The writer is not changed.
4. Done for the runner in #396 (runner-only slice): `state.json` has `schema`
   (`STATE_SCHEMA = 1`), entry validation and the named refusal (`StateFileError`, HTTP 503).
   The runner imports `agent.persist` from the checkout, so the `tools/common` copy of
   `persist.py` is dropped and not made; `calibration.json` is unchanged.
5. `calibration.json` (#608): start with empty offsets and refuse every save when the file
   is newer or unreadable, which includes a zone that is not an object and a `dx` or `dy`
   that is not a number; `schema`.
6. `ActivityStore`: `user_version` and `MIGRATIONS`, step 1 being today's `SCHEMA`.
7. Done in #739. A fixture file per released version for each SQLite store, migrated in a test
   (the harness #396 asks for); #325 is born with it.
8. After #347: delete the copy in `tools/common` and its test.

**Adding a migration N** (the rule slice 7 enforces, for `Memory` and `ActivityStore`): append
the step to `MIGRATIONS`, which bumps `SCHEMA_VERSION`; add `v<N>.sql` next to the others, a
database as version N creates it, with rows that exercise what the step added; pin its sha256 in
the test module (the failure message prints the value). A step should only add. A destructive step
(`DROP`, `DELETE`) makes the older fixtures' row assertions fail; that is the point, and it
belongs in its PR as a deliberate decision, not as a quiet edit of the expected rows. Never edit a fixture that has shipped: the test fails, and the fix is to
restore it from git.

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
   Answered by #252 (ADR 0021): `AGENT_STATE_DIR` is added, `names.json` and `items.json` move under it.
