<!-- Generated from agent/api_schema.json and agent/action_specs.py by `python -m agent.api_contract`; do not edit. -->
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
| `mission` | string | yes |

## `GET /state`

Own stats, spellbook, quest log, equipment and inventory. Also the connection lifecycle (ADR 0015), when the agent has one.

Response: not in game

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |
| `mission` | string | yes |
| `lifecycle` | object |  |
| `lifecycle.state` | string | yes |
| `lifecycle.since` | number | yes |
| `lifecycle.reason` | string | yes |
| `lifecycle.transitions` | array | yes |
| `lifecycle.transitions[].from` | string | yes |
| `lifecycle.transitions[].to` | string | yes |
| `lifecycle.transitions[].reason` | string | yes |
| `lifecycle.transitions[].at` | number | yes |

Response: in game

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `connected` | boolean | yes |
| `mission` | string | yes |
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
| `lifecycle` | object |  |
| `lifecycle.state` | string | yes |
| `lifecycle.since` | number | yes |
| `lifecycle.reason` | string | yes |
| `lifecycle.transitions` | array | yes |
| `lifecycle.transitions[].from` | string | yes |
| `lifecycle.transitions[].to` | string | yes |
| `lifecycle.transitions[].reason` | string | yes |
| `lifecycle.transitions[].at` | number | yes |

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

## `GET /unmet-intents`

Newest unmet-intent records (#319), oldest first, at most 50 in memory: an action the brain named that does not exist, or a Jev cycle whose candidates were idle only. The same records are written to <agent>/unmet/<day>.jsonl. intents is [] when nothing was recorded.

Response: ok

| field | type | always present |
|---|---|---|
| `api_version` | integer | yes |
| `agent` | string | yes |
| `intents` | array | yes |
| `intents[].schema` | integer | yes |
| `intents[].ts` | number | yes |
| `intents[].agent` | string | yes |
| `intents[].mission` | string | yes |
| `intents[].kind` | string | yes |
| `intents[].wanted` | object | yes |
| `intents[].wanted.action` | string/null | yes |
| `intents[].wanted.params` | object | yes |
| `intents[].state` | object | yes |
| `intents[].state.level` | integer/null | yes |
| `intents[].state.position` | object/null | yes |
| `intents[].state.health` | string/null | yes |
| `intents[].state.dead` | boolean | yes |
| `intents[].state.in_combat` | boolean | yes |
| `intents[].state.nearby_units` | integer | yes |
| `intents[].state.quests` | integer | yes |

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
- lifecycle in GET /state is the snapshot of the reconnect supervisor's state machine (docs/adr/0015-agent-connection-lifecycle.md). It is absent on an agent that has none. since and at are unix epoch seconds; transitions holds the newest 32, oldest first.

<!-- BEGIN GENERATED catalog: agent/action_specs.py (scripts/generate.py) -->
## Action catalog

One entry per action spec in `agent/action_specs.py`, in spec order: every action the agent can take, including the ones the death, rest and follow reflexes take. `cap` is the most candidates of that action offered per snapshot (`none`: the spec has no cap). An action marked *Not offered to the LLM* is left out of the LLM's tool list (`LLM_EXCLUDED_ACTIONS` in `agent/action_specs.py`); it stays registered and the other brain may still choose it.

### `set_target`

cap: none

| param | type | required | description |
|---|---|---|---|
| `guid` | string | yes | Snapshot handle (a string like "u3") of the object to target. |

### `face`

cap: none

| param | type | required | description |
|---|---|---|---|
| `guid` | string |  | Snapshot handle (a string like "u3") of the object to face. Mutually exclusive with x/y. |
| `x` | number |  | Target X coordinate. Requires y; mutually exclusive with guid. |
| `y` | number |  | Target Y coordinate. Requires x; mutually exclusive with guid. |

### `move_to`

cap: none

| param | type | required | description |
|---|---|---|---|
| `x` | number | yes | Destination X coordinate. |
| `y` | number | yes | Destination Y coordinate. |
| `z` | number |  | Destination Z coordinate. Optional — omit to stay level with the current height (no terrain data in v1). |
| `stop_distance` | number |  | How close counts as arrived, in yards. Default 1.0. |

### `move_towards`

cap: none

| param | type | required | description |
|---|---|---|---|
| `guid` | string | yes | Snapshot handle (a string like "u3") of the object to move towards. |
| `stop_distance` | number |  | How close counts as arrived, in yards. Default 1.0. |

### `stop_movement`

cap: none

No parameters.

### `auto_attack`

cap: none

| param | type | required | description |
|---|---|---|---|
| `guid` | string | yes | Snapshot handle (a string like "u3") of the unit to attack. |

### `stop_attack`

cap: none

No parameters.

### `idle`

**Not offered to the LLM.**

cap: none

No parameters.

### `cast_spell`

cap: 3

| param | type | required | description |
|---|---|---|---|
| `spell_id` | integer | yes | Spell ID to cast — must be in the agent's spellbook. |
| `target_guid` | string |  | Snapshot handle (a string like "u3") of the unit to cast on. Omit to cast on self. |

### `interact`

cap: 3

| param | type | required | description |
|---|---|---|---|
| `guid` | string | yes | Snapshot handle (a string like "u3") of the NPC or gameobject to interact with. |

### `gossip_select`

cap: 4

| param | type | required | description |
|---|---|---|---|
| `option_index` | integer | yes | The option's `index` from the open gossip window. |
| `code` | string |  | Text for options that require a text-entry box (rare; the window's option marks `coded: true` when needed). |

### `buy_item`

cap: none

| param | type | required | description |
|---|---|---|---|
| `vendor_guid` | string | yes | Snapshot handle (a string like "u3") of the open vendor. |
| `slot` | integer |  | Vendor slot from the open window's items list. |
| `entry` | integer |  | Item entry, as an alternative to slot. |
| `count` | integer |  | How many to buy. Default 1. |

### `sell_item`

cap: 3

| param | type | required | description |
|---|---|---|---|
| `vendor_guid` | string | yes | Snapshot handle (a string like "u3") of the vendor to sell to. |
| `bag` | integer | yes | Bag byte of the item (255 = equipped items/backpack). |
| `slot` | integer | yes | Inventory slot of the item. |
| `count` | integer |  | How many to sell from the stack. Default: whole stack. |

### `train_spell`

cap: 3

| param | type | required | description |
|---|---|---|---|
| `trainer_guid` | string | yes | Snapshot handle (a string like "u3") of the open trainer. |
| `spell_id` | integer | yes | Spell ID from the open trainer window's spells list. |

### `invite_to_group`

cap: none

| param | type | required | description |
|---|---|---|---|
| `name` | string | yes | Exact player name to invite. |

### `accept_group`

cap: none

No parameters.

### `decline_group`

cap: none

No parameters.

### `leave_group`

cap: none

No parameters.

### `promote_leader`

cap: none

| param | type | required | description |
|---|---|---|---|
| `name` | string | yes | Exact name of the group member to make leader. |

### `close_window`

cap: none

No parameters.

### `loot`

cap: 3

| param | type | required | description |
|---|---|---|---|
| `guid` | string | yes | Snapshot handle (a string like "u3") of the lootable corpse/creature. |

### `use_item`

cap: 2

| param | type | required | description |
|---|---|---|---|
| `bag` | integer | yes | Bag byte (255 = equipped items/backpack; see CMSG_USE_ITEM's bag field). |
| `slot` | integer | yes | Slot within that bag (0-18 equipment, 19-22 equipped bags, 23-38 backpack). |
| `target_guid` | string |  | Optional target snapshot handle (a string like "p1"), e.g. a bandage used on an ally. Omit to target self. |

### `destroy_item`

cap: none

| param | type | required | description |
|---|---|---|---|
| `bag` | integer | yes | Bag byte (255 = equipped items/backpack). |
| `slot` | integer | yes | Slot within that bag. |
| `count` | integer | yes | How many to destroy from the stack. |
| `confirm` | boolean | yes | Must be true — a safety guard, not read by the server. |

### `compare_items`

cap: none

| param | type | required | description |
|---|---|---|---|
| `bag_a` | integer | yes | Bag byte of the first item (255 = equipped items/backpack). |
| `slot_a` | integer | yes | Inventory slot of the first item. |
| `bag_b` | integer | yes | Bag byte of the second item (255 = equipped items/backpack). |
| `slot_b` | integer | yes | Inventory slot of the second item. |

### `equip_item`

cap: 3

| param | type | required | description |
|---|---|---|---|
| `bag` | integer | yes | Bag byte of the item (255 = equipped items/backpack). |
| `slot` | integer | yes | Inventory slot of the item. |

### `accept_quest`

cap: none

| param | type | required | description |
|---|---|---|---|
| `npc_guid` | string | yes | Snapshot handle (a string like "u3") of the questgiver NPC. |
| `quest_id` | integer | yes | Quest ID to accept, from the open quest_list/quest_details/gossip window. |

### `complete_quest`

cap: none

| param | type | required | description |
|---|---|---|---|
| `npc_guid` | string | yes | Snapshot handle (a string like "u3") of the questgiver NPC. |
| `quest_id` | integer | yes | Quest ID to complete, from quest_log. |

### `turn_in_quest`

cap: none

| param | type | required | description |
|---|---|---|---|
| `npc_guid` | string | yes | Snapshot handle (a string like "u3") of the questgiver NPC. |
| `quest_id` | integer | yes | Quest ID to turn in. |
| `reward_choice` | integer |  | Index of the reward item to pick, from the open window's reward_choice_items. Default 0. |

### `abandon_quest`

cap: 2

| param | type | required | description |
|---|---|---|---|
| `slot` | integer | yes | Quest log slot (0-24), from quest_log's `slot` field — not the quest id. |

### `open_trade`

cap: none

| param | type | required | description |
|---|---|---|---|
| `guid` | string | yes | Snapshot handle (a string like "u3") of the player to trade with. |

### `accept_trade_request`

cap: none

No parameters.

### `offer_item`

cap: none

| param | type | required | description |
|---|---|---|---|
| `bag` | integer | yes | Bag byte of the item (255 = equipped items/backpack). |
| `slot` | integer | yes | Inventory slot of the item. |
| `trade_slot` | integer |  | Which of the 6 trade slots (0-5) to put it in. Default: the first free one. |

### `offer_gold`

cap: none

| param | type | required | description |
|---|---|---|---|
| `amount` | integer | yes | Copper to offer (100 copper = 1 silver, 10000 = 1 gold). 0 clears a previous offer. |

### `accept_trade`

cap: none

| param | type | required | description |
|---|---|---|---|
| `expected_their_gold` | integer |  | Gold you believe the other side has offered. Default 0. |
| `expected_their_item_entries` | array of integer |  | Item entries you believe the other side has offered (any order). Omit/empty if you believe they're offering no items. |

### `cancel_trade`

cap: none

No parameters.

### `open_mailbox`

cap: none

No parameters.

### `send_mail`

cap: none

| param | type | required | description |
|---|---|---|---|
| `to` | string | yes | Recipient character name. |
| `subject` | string | yes | Mail subject. |
| `body` | string | yes | Mail body text. |
| `gold` | integer |  | Copper to send. Default 0. |
| `bag` | integer |  | Bag byte of an item to attach (255 = equipped items/backpack). Omit to send no item. |
| `slot` | integer |  | Inventory slot of the item to attach — required together with bag. |

### `take_mail`

cap: none

| param | type | required | description |
|---|---|---|---|
| `mail_id` | integer | yes | Mail id, from the open mailbox window's `mails` list. |

### `delete_mail`

cap: none

| param | type | required | description |
|---|---|---|---|
| `mail_id` | integer | yes | Mail id, from the open mailbox window's `mails` list. |

### `release_spirit`

cap: none

No parameters.

### `reclaim_corpse`

cap: none

No parameters.

### `follow`

cap: 2

| param | type | required | description |
|---|---|---|---|
| `player_name` | string | yes | Name of a nearby player to follow (case-insensitive). |
| `distance` | number |  | How close to stay, in yards. Default 3.0. |

### `assist`

cap: none

| param | type | required | description |
|---|---|---|---|
| `on` | boolean | yes | True to assist, false to stop assisting. |

### `stop_following`

cap: none

No parameters.

### `rest`

cap: none

No parameters.

<!-- END GENERATED catalog -->
