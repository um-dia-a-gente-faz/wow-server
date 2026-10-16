# Local dev stack

One command brings up the whole pipeline (agent action -> audit -> API -> site) on
your machine with no live realm, no `.env` and no credentials (#389). Needs Docker
with the compose plugin.

```bash
dev/dev.sh up      # build + start; wowmap on http://127.0.0.1:9400
dev/dev.sh logs agent
dev/dev.sh smoke   # boot, wait for an agent decision in wowmap's API, tear down
dev/dev.sh down    # stop and delete the volumes
```

Everything lives under `dev/`; it never touches `docker-compose.yml`, `scripts/` or the
deploy workflow.

| Service | What | Port (127.0.0.1) |
|---|---|---|
| `world-mock` | `tools/world_mock`, fake auth + world server | internal |
| `jev-mock` | `tools/jev_mock`, random Decisions API | internal |
| `agent` | one real agent (Luaprata) on `AGENT_BRAIN=jev` against the two mocks | 9601 |
| `chat-feed` | `tools/chat-feed` on an empty log | 9500 |
| `database` | MySQL seeded from `dev/fixture/*.sql` | internal |
| `wowmap` | `tools/wowmap`, reads the fixture DB, the agent API and the shared audit volume | 9400 |

The agent connects as the mock's built-in made-up account (`MOCKUSER` / `mockpass`).
The jev-mock answers are random: never point live agents at it.

## What the stack does not have

wowmap runs without the client DBC files and map art (they are proprietary and not in
the repo), so the map page loads but has no tiles and the position transform is
uncalibrated. The API, the agent mind tab and the activity feed work. The mocks play
one scripted scenario (see `tools/world_mock/README.md`), so the agent mostly idles.

## Refreshing the fixture

`dev/fixture/01-fixture.sql` covers the character row, the players list, the account
lookup and the activity snapshot poller (so it holds `auth.account`, `characters.characters`,
`character_inventory`, `item_instance(guid, itemEntry, count)`,
`character_queststatus_rewarded`, `world.item_template(entry, name, SellPrice)` and
`world.quest_template`; each is read by `tools/wowmap/wowmap/activity.py` or `repo/*.py`).

It does **not** cover the inspect drawer's inventory, talents, reputation, stats and
achievements: those need more `item_instance`/`item_template` columns and the
`character_talent`, `character_reputation`, `character_stats` and `character_achievement`
tables. They come back empty and wowmap logs `best_effort` warnings; that is expected.

To widen it, add the table or column to the `CREATE TABLE` (names from TrinityCore
`sql/base/characters_database.sql`) and re-run `dev/dev.sh down && dev/dev.sh up`. Init
scripts run only on an empty database, which `down` guarantees by deleting the volumes.

Ports and variable names come from `agent/config.py::SETTINGS` and each tool's README;
the compose file sets only the values that differ from the defaults.

## Not done yet

- The Console v4 backend (#323) joins when it exists.
- `dev-smoke` (`.github/workflows/dev-smoke.yml`) runs `smoke` on every push to `main` (#481).
