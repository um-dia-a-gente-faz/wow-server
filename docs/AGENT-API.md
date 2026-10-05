<!-- Generated from agent/api_schema.json by `python -m agent.api_contract`; do not edit. -->
# Agent observability API (api_version 1)

Shapes of the JSON served by agent/http_api.py and read by tools/wowmap and tools/agent-runner. Subset of JSON Schema (type, properties, required, items, enum): unknown fields are always allowed. Every response carries api_version. Change a shape additively and keep api_version; a breaking change bumps api_version here, in agent/http_api.py and in the consumers' supported set (tests pin all three together).

## `GET /healthz`

Liveness: is the process up, and is a game session attached.

Response: ok

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `ok` | boolean | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |
| `uptime_s` | number | yes |

## `GET /state`

Own stats, spellbook, quest log, equipment and inventory.

Response: not in game

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |

Response: in game

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |
| `self` | object | yes |
| `self.name` | string/null | yes |
| `self.guid` | integer/null | yes |
| `self.race` | integer/null | yes |
| `self.class` | integer/null | yes |
| `self.level` | integer/null | yes |
| `self.health` | integer/null |  |
| `self.max_health` | integer/null |  |
| `self.power` | object |  |
| `self.max_power` | object |  |
| `self.is_dead` | boolean |  |
| `self.is_ghost` | boolean |  |
| `self.xp` | integer |  |
| `self.next_level_xp` | integer |  |
| `self.money` | integer/null | yes |
| `self.position` | object |  |
| `self.position.map` | integer | yes |
| `self.position.x` | number | yes |
| `self.position.y` | number | yes |
| `self.position.z` | number | yes |
| `spellbook` | array | yes |
| `spellbook[].id` | integer | yes |
| `spellbook[].name` | string/null | yes |
| `quest_log` | array | yes |
| `equipment` | object | yes |
| `inventory` | array | yes |

## `GET /perception`

WorldState.snapshot(): the same view the LLM gets, GUIDs as short handles.

Response: not in game

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |

Response: in game

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |
| `position` | object/null | yes |
| `is_dead` | boolean |  |
| `is_ghost` | boolean |  |
| `corpse_position` | object/null |  |
| `nearby_units` | array | yes |
| `nearby_units[].name` | string/null |  |
| `nearby_units[].type` | string/null |  |
| `nearby_units[].entry` | integer/null |  |
| `nearby_units[].level` | integer/null |  |
| `nearby_units[].health_pct` | number/null |  |
| `nearby_units[].in_combat` | boolean |  |
| `nearby_units[].distance` | number | yes |
| `nearby_players` | array | yes |
| `nearby_players[].name` | string/null |  |
| `nearby_players[].distance` | number | yes |
| `nearby_objects` | array | yes |
| `nearby_objects[].name` | string/null |  |
| `nearby_objects[].distance` | number | yes |
| `window` | object/null |  |
| `trade` | object/null |  |
| `pending_invite` | object/null |  |
| `chat_inbox` | array | yes |
| `channels` | array |  |
| `quest_log` | array |  |
| `equipment` | object |  |
| `inventory` | array |  |

## `GET /brain`

Goal, brain (jev/llm), model, last decisions, action history, reflexes, token usage. Query: n = number of decisions.

Response: ok

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |
| `goal` | string/null | yes |
| `brain` | string/null | yes |
| `model` | string/null | yes |
| `cycle` | integer/null | yes |
| `decisions` | array | yes |
| `decisions[].seq` | integer | yes |
| `decisions[].cycle` | integer/null |  |
| `decisions[].ts` | number/null |  |
| `decisions[].brain` | string/null |  |
| `decisions[].confidence` | number/null |  |
| `decisions[].fallback` | string/boolean/null |  |
| `decisions[].tool_call` | object/null |  |
| `decisions[].result` | object/null |  |
| `history` | array | yes |
| `reflexes` | object | yes |
| `tokens` | object | yes |
| `tokens.prompt_total` | integer | yes |
| `tokens.completion_total` | integer | yes |
| `tokens.cycles` | integer | yes |
| `tokens.since` | number | yes |

## `GET /`

Endpoint index.

Response: ok

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `endpoints` | array | yes |

## `POST /control/walk`

Operator walk (only with AGENT_CONTROL_TOKEN set). Success, refusal and error bodies all carry api_version.

Response: any

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `ok` | boolean |  |
| `outcome` | string |  |
| `code` | string |  |
| `error` | string |  |

## Errors

Any non-2xx JSON body: {api_version, error}.

## Notes

- GET /events is a Server-Sent Events stream (event: event | decision, data: JSON) and is not versioned per message.
- A missing api_version means an agent older than this contract; consumers treat it as version 1 (the shape did not change).
