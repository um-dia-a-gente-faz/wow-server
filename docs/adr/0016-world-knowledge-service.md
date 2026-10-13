# ADR 0016: One read-only world-knowledge source (a bundle file, not a service)

## Status

Proposed (#598, the ADR part of #340). The owner accepts it later. Docs only: no code, config
or compose file changes with this ADR, and nothing is built before it is accepted.

ADR numbers: #340 calls this "ADR 0013". That number is taken on `main` by the admin-only GM
control ADR, so this is 0016. The file name says "service" because #598 fixed it; the decision
below is a file, not a service.

## Context

Files that do not exist yet are written as a module name and its directory (for example
"`worldknowledge.py` in `agent/`"), because `scripts/tests/test_docs.py` requires every
backticked repository path in a doc to exist. Every full path in this ADR is therefore a
file on `main`.

"World knowledge" here means static game data: facts that change only when the client build
or the world database (TDB) changes. Spell names and costs, creature templates, where an NPC
spawns, what a vendor sells. It does not mean live state (who is online, a character's
inventory), which stays where it is.

#340 says this data is reached by three unrelated routes and that each new feature adds a
fourth. Each claim was checked against `main`; the result is below, with what was stale.

### What the agent has today

- **Templates over the wire, cached per container.** `agent/names.py::NameCache` asks the
  server for creature and gameobject templates (`build_creature_query`,
  `build_gameobject_query`) and saves the found ones to `DEFAULT_CACHE_PATH`,
  `~/.cache/wow-agent/names.json`. The path in #340 is correct.
  `agent/perception/world.py::WorldState.__init__` builds it with the default path, and
  `agent/session.py` calls `self.world_state.names.load()` at login.
- **The item cache is neither written nor read by a running agent.** `agent/items.py::ItemCache`
  queries item templates the same way. It used to save them to `~/.cache/wow-agent/items.json`;
  since #712 no running agent writes that file (`save` and `load` are kept for a future
  decision, and an existing file is left alone). `ItemCache.load` has no caller outside the tests: `agent/session.py` loads names only. So
  item templates are re-queried in every process, even one that restarts in the same
  container. #340 does not mention this cache.
- **The name cache is weaker than #340 says.** `docker-compose.agents.yml` mounts one volume into
  an agent container, the audit directory (`x-agent` → `volumes`). `~/.cache` is in the
  container's writable layer, so the cache is private to one container and is lost whenever
  the container is recreated, which is every new image tag. So "25 agents repeat the same
  queries" is true, and they also repeat them after each agent deploy.
- **The name cache is never invalidated.** The server announces its template cache version at
  login: `WorldSession::InitializeSessionCallback` calls `SendClientCacheVersion`
  (`SMSG_CLIENTCACHE_VERSION`, 0x4AB, one `uint32`), whose value is `world.version.cache_id`
  unless `ClientCacheVersion` in the worldserver config overrides it (`World::LoadDBVersion`
  and `World::LoadConfigSettings` in `World.cpp`, TrinityCore branch `3.3.5`). A real client
  drops its template cache when this number changes. `agent/opcodes.py` does not define the
  opcode, so the agent ignores it and trusts `names.json` forever. (`items.json` cannot be
  stale, because it is never read.)
- **A hand-copied table is already the fourth route.** `agent/spells.py::SPELL_TABLE` holds 17
  spells typed in by hand, read through `get_spell_info` by `agent/think.py`,
  `agent/http_api.py` and `agent/actions/combat.py`. Its own comment says to "verify against
  Spell.dbc directly before trusting anything past ~level 5", and it has flat mana costs
  where 3.3.5a `Spell.dbc` also carries a percent-of-base-mana cost (`ManaCostPct`, field 204
  of `SpellEntry` in `DBCStructure.h`). `agent/known_targets.py` is a second hand list (the
  quest giver, vendor and mobs of Sunstrider Isle), by name only and for probes. #340 does
  not list either; they are the clearest evidence for its claim.
- **No database access, by construction.** #340 says CLAUDE.md states "no DB access". It
  does not: it says `agent/` is stdlib-only. The stdlib has no MySQL driver, and
  `agent/tests/test_architecture.py::test_agent_is_stdlib_only` rejects any third-party
  import, so the game database is unreachable in practice. This ADR makes the rule explicit
  (see Decision).
- **`sqlite3` is already used in `agent/`.** `agent/memory.py` (ADR 0011) is a stdlib
  `sqlite3` store, pinned in `LAYERS` as `"memory": set()`.

### What the console has today

- `tools/wowmap` is still a stdlib `http.server` site: `tools/wowmap/wowmap/app.py` runs a
  `ThreadingHTTPServer` with `common.jsonserver.JsonHandler`, and `state.db()` returns a
  `pymysql` connection. #340 says "soon FastAPI per #323". FastAPI is decided (ADR 0012,
  Accepted) but not on `main`; #323 is open.
- From the `world` database it reads two tables only: `world.item_template`
  (`repo/characters.py`, `activity.py::ITEM_NAMES_SQL`) and `world.quest_template`
  (`activity.py::QUEST_NAMES_SQL`). Nothing in `tools/`, `exporters/`
  or `scripts/` reads spawns, quest givers or vendors.
- It connects as MySQL `root` (`MYSQL_USER=root` in `monitoring/docker-compose.yml`).
- Client data comes from `tools/dbc` (`wdbc.py::WdbcFile`, `names.py::GameNames`), reading
  the DBC files at `/opt/wowmap-data/dbc` on the VM. The DBCs are not in the repository.

### What the navmesh sidecar has today

Nothing. #82 proposes a pathfinder tool under `tools/`; no such directory exists on `main`.
#82 (the sidecar) and #312 (the mover that uses it) are open. #340 writes "(#82, M9 #312)"
as if both were the sidecar; #312 is the consumer.
#82's data is the movement maps in the `server_data` volume of the wow-server VM, its
preferred implementation is C++ linked against Recast/Detour, and it must answer in under
50 ms at p95.

### Other facts that shape the choice

- `agent/api_contract.py` is what #340 says: the stdlib validator, fixture generator and doc
  renderer for `agent/api_schema.json` (ADR 0010). It describes the agent's own HTTP API.
- The agent image is built from the repository (`Dockerfile`: `COPY agent/ ./agent/`), by CI
  and on the agent host. Neither has the DBCs or the world database.
- Agents run on the `wow-agents` VM, the database and DBCs are on the wow-server VM
  (ADR 0002). Each agent container has `mem_limit: 128m`.
- The world data changes only on purpose: the TDB version is tied to the server image tag
  (`tdb/README.md`).
- #340 cites "decision 2 in `docs/AGENT-DIRECTION.md`: agents should behave like players".
  Decision 2 is "Agents decide for themselves". The like-a-player rule is in decision 1
  ("the server treats an agent like any other player"), in `GLOSSARY.md` (**Agent**: "plays
  on the realm like any other player") and, most directly, in the glossary entry for
  **Level to cap**: "finding quest givers by walking the map". Decision 4 adds "no shared
  state between agent processes" and decision 5 "no memory is shared between agents".

## The fairness question, answered first

It decides more than the transport does. Static data falls into two tiers:

| Tier | What | Who has it | Examples |
|---|---|---|---|
| **client** | data shipped inside the 3.3.5a client | every human player | spell names, ranks, costs, ranges (`Spell.dbc`), area and map names, flight points (`TaxiNodes.dbc`) |
| **server** | rows of the `world` database the server never sends unasked | a player only through an outside site or a database addon | spawn positions, which NPC starts a quest, vendor stock, loot tables, respawn times |

Templates the server sends on request (creature, gameobject, item, quest) are neither: the
agent already gets them the way a client does.

The client tier raises no fairness question. The server tier contradicts the glossary's
**Level to cap** as written, so it is off unless the owner turns it on (open question 1).
This split means the first slice needs no answer to the fairness question and no database.
Its PR still waits for the owner's go-ahead for another reason: it adds a line to
`scripts/check.sh` (see Consequences).

## Options considered

### A. Read-only HTTP service on the wow-server VM (#340 option 1)

A new `tools/` service with `pymysql` and `tools/dbc`, queried by agents over the LAN.

- **Cost.** A new container in a compose project on the wow-server VM (a deploy-path PR that
  waits for the owner), database credentials for it (`root` like wowmap, or a new read-only
  MySQL user), an HTTP client in `agent/` with timeout and fallback, a cache, a port.
- **What it breaks.** Nothing at once, but every agent decision that uses knowledge gains a
  network hop across two VMs to a service that any merge to `main` may rebuild. It adds a
  failure mode (slow, not down) that a file does not have.
- **What it buys.** Freshness. The data changes only when the TDB changes, so this is worth
  little.
- **Undo.** Remove the service, its compose entry, the client and the settings: four places.

### B. Versioned flat bundle (#340 option 2)

Export the data once into a file; the agent reads the file. Two variants:

- **B1, baked into the agent image**, as #340 words it. Not possible as stated: the image is
  built where neither the DBCs nor the database exist. Making it possible means committing
  the bundle, which puts client-derived data and a binary of several MB in git.
- **B2, a file on the host, mounted read-only**, the way `/opt/wowmap-data` already reaches
  wowmap. **Cost:** one exporter run by hand and one file copy per TDB or client-data
  change. **What it breaks:** nothing; an absent file is today's behaviour. **Stale until
  rebuilt**, which can be detected (see Decision, Invalidation). **Undo:** delete the file
  and one module.

Format: JSON would be parsed into every one of 25 processes that each have 128 MB. SQLite is
read on demand through the stdlib, is indexed, and one host file mounted into 25 containers
is cached once by the host kernel.

### C. Fold it into the navmesh sidecar (#340 option 3)

- **Cost.** The knowledge would wait for, and fail with, a component that does not exist and
  that #82 calls "the biggest open risk". Its data (binary navmesh tiles), language (C++)
  and latency budget have nothing in common with row lookups.
- **What it breaks.** Path failures and knowledge failures become one failure.
- **Undo.** Hard: two contracts in one service have to be pulled apart.

### D. Status quo: protocol only, plus hand tables

Free today. It is how `SPELL_TABLE` got 17 unverified rows, and M9 would add more.

## Decision

**Option B2.** World knowledge is one read-only SQLite file, the **world bundle**, built by a
hand-run exporter under `tools/` and read by one stdlib module in `agent/`.

1. **Reader.** A new module, `worldknowledge.py` in `agent/`, is the only code in `agent/`
   that opens the bundle. It opens it read-only and imports nothing else from `agent/`.
   `agent/` still never connects to the game database; this ADR states that rule in words
   for the first time. A read-only data file is not database access, in the same way the
   client's DBC files are not.
2. **Exporter.** A new tool, `worldknowledge` under `tools/`, builds the bundle from the DBC
   directory and, for server-tier tables only, from the `world` database. It runs by hand
   where those exist (the wow-server VM). It is not a service, has no port and is in no compose file.
3. **Contract owner.** `agent/` holds the contract, as in ADR 0010; there `agent/` is the
   producer, here it is the consumer. The reader module holds
   `SCHEMA_VERSION` and the table definitions. The exporter imports them, so there is one
   definition. A contract test builds a bundle with the real exporter from fixture data and
   reads it back with the real reader; this is stronger than ADR 0010's consumer tests,
   which do not read fields. The file's version stamp is `PRAGMA user_version`, as in
   ADR 0011. Versioning of persisted files follows ADR 0018 once that exists.
4. **Tiers.** Every table is declared `client` or `server` in the bundle. The reader answers
   server-tier lookups only when it was constructed with them allowed; otherwise they return
   unknown. The default is not allowed.
5. **Failure.** No file, a corrupt file, or a `user_version` the code does not know gives an
   unavailable reader: every lookup returns `None`, with one warning, and the agent plays as
   it does today. No lookup raises. There is no network, so there is no timeout to tune.
6. **Advisory only**, as in ADR 0011: a value from the bundle is a hint. What the server
   says (a cast failure, an NPC that is not there) wins.
7. **Invalidation.** The bundle records what it was built from: a hash of each source DBC,
   and for server-tier tables `world.version.db_version` and `cache_id`. Client-tier tables
   are valid for as long as the client build is 12340. For server-tier tables the agent
   compares the bundle's `cache_id` with the value of `SMSG_CLIENTCACHE_VERSION`; on a
   mismatch they return unknown. That check catches a changed cache id, not every TDB
   change, so the operating rule is also written down: rebuild the bundle whenever the TDB
   file or the server image tag changes.
8. **The wire route of `agent/names.py` and `agent/items.py` is kept**, on purpose. Asking
   the server for templates is the client's own mechanism, it asks the running server, and
   its cost is bounded (`budget_per_second`, and `MAX_PENDING` in `names.py`). Backing it
   with the bundle would save a few small queries per login and add a way to be stale. The
   two differ on disk today: `names.json` is read back at login, `items.json` is not (see
   Context). This ADR does not decide whether `ItemCache.load` gets a caller; that is a fix
   of its own, and if it is made, `items.json` needs the same invalidation as `names.json`
   (further slice 3). Template caches stay per agent:
   one writable cache shared by all agents would be shared state between agent processes
   (decision 4). The bundle is read-only and identical for everyone, like installed client
   data, so it is not.
9. **Console.** `tools/wowmap` keeps its database connection; it needs live character data
   that no bundle can hold. It is not migrated. When #323 builds the query layer, its
   static-world lookups use the bundle's table and column names, so the two do not drift.
10. **Navmesh sidecar.** It stays a separate service (#82) and is not a consumer of the
    bundle. What it shares is the rule in point 5: unreachable means unknown, and the agent
    falls back. If it later needs row data, it reads the bundle.

### Why the others lose

- **A** adds the largest runtime dependency of the four to buy freshness for data that
  changes only on a deliberate TDB update. If a service is ever needed (many more tables,
  or consumers that cannot mount a file), it would serve these same tables, so B2 is its
  first step and nothing is thrown away.
- **B1** needs data in git that the repository has kept out so far.
- **C** couples the easy problem to the hardest unbuilt one.
- **D** is what #340 was opened to stop.

## Consequences

- One more file to keep on the agent host, and one manual step after a TDB change. Forgetting
  it costs stale server-tier hints (detected in part, point 7), never a crash.
- The bundle reaches agent containers through a read-only mount, which is a change to
  `scripts/gen_agents_compose.py` and the runner's generated compose file. That is a later
  slice and waits for the owner like any `scripts/` change.
- `SPELL_TABLE` can be retired once the bundle is mounted; until then it is the fallback.
- The new directory under `tools/` triggers no deploy (`scripts/deploy.sh` does not
  match it), so building it never disconnects players. The first slice also adds one
  `SUITES` line to `scripts/check.sh`, so its PR is a `scripts/` change and follows the rule
  for those in `CLAUDE.md` (*Working model*): it waits for the owner's go-ahead, although no
  stack is redeployed.
- #315 is unchanged: quest givers are still found by walking, because that is server-tier
  knowledge and it is off. #82 is unchanged. Both get a pointer to this ADR on acceptance.
- #252's "shared caches" item is answered for static data (the bundle); template caches stay
  per agent by point 8.

## First slice (ready issue outline)

**Title:** world bundle slice 1: read-only reader and the spell table exporter

**What to build.** The reader and the exporter for one client-tier table, `spell`, with the
contract test between them. No consumer is wired in: `agent/spells.py` is not touched, no
setting is added, no compose file changes. Needs no database.

**Files**

- `worldknowledge.py` in `agent/` (new, stdlib only, imports nothing from `agent/`):
  - `SCHEMA_VERSION = 1` and `SCHEMA`, the `CREATE TABLE` statements for `meta` (key, value),
    `tables` (name, tier) and `spell` (`id` primary key, `name`, `rank`, `spell_level`,
    `power_type`, `mana_cost`, `mana_cost_pct`, `cast_time_ms`, `range_min_yd`,
    `range_max_yd` (the hostile range) and `range_max_friendly_yd`).
  - `class WorldKnowledge`: `__init__(self, path: str | None, allow_server_tier: bool = False)`,
    attribute `available: bool`, `meta() -> dict`, `spell(spell_id: int) -> dict | None`,
    `close()`. Opens with
    `sqlite3.connect("file:...?mode=ro", uri=True, check_same_thread=False)` and holds one
    `threading.Lock` around every use of the connection, which makes it safe to call from
    any thread. `agent/memory.py::_connect` passes `check_same_thread=False` for the same
    reason, and `Memory` guards its reader with a lock. Without the flag, a lookup from a
    second thread raises `sqlite3.ProgrammingError`, which would break "no exception leaves
    the module".
- `test_worldknowledge.py` in `agent/tests/` (new).
- `agent/tests/test_architecture.py`: add `"worldknowledge": set()` to `LAYERS`.
- `build.py` in a new directory `worldknowledge` under `tools/`:
  `build(dbc_dir: str, out_path: str) -> dict` (row count per table) and a CLI taking
  `--dbc-dir DIR --out FILE`. Reads `Spell.dbc`, `SpellCastTimes.dbc` and `SpellRange.dbc` through
  `tools/dbc/wdbc.py`. Field indexes come from `SpellEntry`, `SpellCastTimesEntry` and
  `SpellRangeEntry` in TrinityCore `DBCStructure.h` (branch `3.3.5`), checked against the
  format strings in `DBCfmt.h` and cited in the module docstring, as `tools/dbc/names.py`
  does. Writes to a temporary file and renames it.
- `tests/test_build.py` next to it (new).
- `tools/tests/test_architecture.py`: `ROOTS`, `PACKAGE_EDGES["worldknowledge"] = {"dbc"}`,
  `AGENT_EDGES["worldknowledge"] = {"worldknowledge"}`.
- `scripts/check.sh`: one line in `SUITES` for the new suite. This makes the PR a `scripts/`
  change (owner's go-ahead, `CLAUDE.md` *Working model*).
- `docs/ARCHITECTURE.md`: the two modules in the module map.

**Behaviour**

- A lookup of a known id returns a dict with the columns above; an unknown id returns `None`.
- `path` is `None`, missing, not a SQLite file, or has another `user_version`: `available`
  is `False`, every lookup returns `None`, `meta()` returns `{}`, one warning is logged, no
  exception leaves the module. A missing file is not created.
- The reader never writes: the file's bytes are unchanged after use.
- The exporter refuses a DBC whose field count is not the expected one and leaves any
  existing output file untouched.

**Done when** (each item is a test in the named suite, or the command given)

- [ ] Known and unknown spell lookups: `scripts/check.sh test-agent`.
- [ ] Each of the four unavailable cases above, with exactly one warning and no exception:
      `scripts/check.sh test-agent`.
- [ ] A lookup made from a second thread (not the one that built the reader) returns the
      row and raises nothing: `scripts/check.sh test-agent`.
- [ ] The reader opens read-only: a write through its connection fails and a missing path is
      not created: `scripts/check.sh test-agent`.
- [ ] Contract test: a bundle built by `build()` from fixture WDBC bytes is read by
      `WorldKnowledge`, and every column of a fixture spell matches, cast time and range
      resolved through their index tables: `scripts/check.sh test-worldknowledge`.
- [ ] A wrong field count fails the build and keeps the previous output file:
      `scripts/check.sh test-worldknowledge`.
- [ ] Both architecture suites pass with the new entries:
      `scripts/check.sh test-agent test-tools-arch`.
- [ ] `scripts/check.sh all` passes.
- [ ] Human step, left unticked: run the exporter on the VM against `/opt/wowmap-data/dbc`,
      then compare the 17 rows of `SPELL_TABLE` with the bundle and record every difference
      in the issue. That list is the input to slice 2.

## Further slices

1. **Done in #742.** `agent/spells.py::get_spell_info` reads the bundle first and `SPELL_TABLE`
   second; the bundle path is the setting `AGENT_WORLD_BUNDLE` in `agent/config.py::SETTINGS`
   (empty = off). Narrowed: the set of spells does not change yet. An id outside `SPELL_TABLE`
   still gives `None`, bundle or not; for an id in the table the bundle corrects
   `power_type`, `cast_time_ms`, `range_yd`, `min_level` and (unless `mana_cost_pct` > 0)
   `power_cost`; the table's `name` stays (the bundle keeps the rank apart, so 133 and 143 would
   collide) and a name difference is only logged. `root.load_world_bundle` passes `spells` a lookup
   callable, so `spells` imports nothing new. Which bundle spells an agent may consider is a later decision.
2. Mount the bundle read-only into agent containers (`scripts/gen_agents_compose.py`, the
   runner) and document the rebuild-and-copy step in `docs/DEPLOYMENT.md`.
3. Handle `SMSG_CLIENTCACHE_VERSION`: keep the cache id and discard `names.json` when it
   changes; the same for `items.json` if `ItemCache.load` has a caller by then.
4. More client-tier tables as M9 issues need them (`AreaTable`, `Map`, `TaxiNodes`).
5. Server-tier tables (spawns, quest starters, vendors) from the `world` database, only if
   open question 1 is answered yes.
6. #323's query layer adopts the bundle's table and column names for static-world lookups.

## Open questions

Each has a default, so no work waits on the answer.

1. **May agents use server-tier knowledge at all?** Default: no. The glossary says a
   level-to-cap agent finds quest givers by walking. If yes, the proposal is per mission
   kind, never global, and never loot tables or respawn times.
2. **Is a read-only data file compatible with "`agent/` has no database access"?** Default:
   yes, as point 1 of the Decision argues. If no, option A is the only route and the first
   slice changes.
3. **Where does the bundle live on the agent host?** Default:
   `/opt/wow-agent-runtime/world/world.sqlite3`, outside the checkout (ADR 0002, D5).
4. **Who copies the bundle from the wow-server VM to `wow-agents`?** Default: the owner, by
   hand, after a TDB or image-tag change. The audit pull of ADR 0002 goes the other way, so
   automating this needs a new key and is not proposed.
5. **Should the bundle ever be committed?** Default: no; it is derived from client data the
   repository does not hold.
6. **Should `names.json` get a volume so it survives a recreate, and should `items.json`
   be read at all?** Today only `names.json` is read back. Default: decide both with the
   volume question of ADR 0011; not part of this ADR.
