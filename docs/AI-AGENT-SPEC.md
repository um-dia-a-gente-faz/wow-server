# AI Agent Gameplay — World of Warcraft (3.3.5a)

Specification for enabling autonomous AI agents to play World of Warcraft on a
TrinityCore private server. Agents perceive the game world, make decisions, and
execute actions through a structured API layer — no screen scraping, no bot
detection evasion, no cheating. This is a research platform for embodied AI
agents in a rich, persistent virtual world.

## Vision

AI agents that:

- Create characters and progress from level 1 to 80
- Navigate the open world (pathfinding, flight paths, boats)
- Accept and complete quests (read quest text, track objectives)
- Engage in combat (target selection, ability rotation, movement)
- Manage inventory, equipment, and talents
- Interact with NPCs (vendors, trainers, quest givers)
- Group with other agents or human players
- Use the auction house and in-game economy
- Learn and adapt (memory of places, NPCs, strategies)

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Agent Host                                │
│                                                                  │
│  ┌──────────────────────┐      ┌──────────────────────────────┐ │
│  │   AI Agent (LLM)     │      │   Agent Runtime (Python)     │ │
│  │                      │◄────►│                              │ │
│  │  - Reasoning         │      │  - World state cache         │ │
│  │  - Planning          │      │  - Action queue              │ │
│  │  - Memory            │      │  - Perception pipeline       │ │
│  │  - Decision making   │      │  - Safety/rate limits        │ │
│  └──────────────────────┘      └──────────┬───────────────────┘ │
│                                           │                      │
└───────────────────────────────────────────┼──────────────────────┘
                                            │ WoW Client Protocol
                                            │ (TCP 3724 + 8085)
                                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                  TrinityCore Server (pandora)                     │
│                                                                  │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ Auth Server │  │ World Server │  │  Agent API Layer (NEW)   │ │
│  │  :3724      │  │  :8085       │  │  :8090 (HTTP + WS)       │ │
│  │             │  │              │  │                          │ │
│  │ Account     │  │ Game logic   │  │  - Perception (REST)     │ │
│  │ Session     │  │ Entities     │  │  - Actions (REST)        │ │
│  │ Realm list  │  │ Maps/Navmesh │  │  - Events (WS)           │ │
│  └─────────────┘  └──────────────┘  │  - State sync            │ │
│                                      └──────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### Why an API layer instead of a game client?

Agents don't need to render graphics, play sounds, or mimic human input timing.
A direct API layer gives agents:

- **Structured perception** — JSON instead of pixels. An agent gets its
  position, nearby entities, quest log, and inventory as typed data.
- **Deterministic actions** — no pixel-hunting, no OCR, no coordinate guessing.
- **Speed control** — agents can think at their own pace without real-time
  pressure (or can be time-budgeted for realism).
- **Observability** — every agent action and world state delta is loggable.

## Agent Perception (Read API)

An agent perceives the world through structured queries. Every perception call
is scoped to what a real player *could* see from their character's perspective.

### GET /agent/{id}/state

Full character snapshot.

```json
{
  "character": {
    "guid": 1, "name": "Agent_01", "race": 1, "class": 4,
    "level": 12, "xp": 8400, "xp_to_level": 11400,
    "position": {"map": 0, "x": -8945.3, "y": -132.7, "z": 83.5, "o": 2.14},
    "health": 342, "max_health": 342,
    "mana": 890, "max_mana": 890,
    "money": 23450,
    "state": "standing"
  },
  "equipment": [
    {"slot": 0, "entry": 3740, "name": "Decapitating Sword", "quality": 2}
  ],
  "inventory": [
    {"bag": 0, "slot": 0, "entry": 2589, "name": "Linen Cloth", "count": 14}
  ],
  "quest_log": [
    {"id": 2158, "title": "Rest and Relaxation", "status": 1,
     "objectives": [{"type": "kill", "entry": 1234, "count": 5, "needed": 10}]}
  ],
  "talents": {"points": 5, "trees": [{"id": 0, "spent": 3}, {"id": 1, "spent": 2}]},
  "spellbook": [{"id": 6673, "name": "Battle Shout", "rank": 1}],
  "cooldowns": [{"spell_id": 6673, "remaining_ms": 4500}]
}
```

### GET /agent/{id}/perception

What the agent currently sees/hears. Scoped to visible range and line-of-sight.

```json
{
  "position": {"map": 0, "x": -8945.3, "y": -132.7, "z": 83.5},
  "nearby_units": [
    {"guid": 10342, "entry": 1547, "name": "Dire Wolf",
     "type": "creature", "faction": 14, "level": 11,
     "position": {"x": -8920.1, "y": -120.3, "z": 83.3},
     "distance": 27.4, "health_pct": 0.82,
     "in_combat": false, "target_guid": 0}
  ],
  "nearby_objects": [
    {"guid": 50201, "entry": 176785, "name": "Ammo Crate",
     "type": "gameobject",
     "position": {"x": -8938.0, "y": -140.2, "z": 83.5},
     "distance": 10.1}
  ],
  "nearby_players": [],
  "chat_messages": [],
  "minimap_pois": []
}
```

### Additional perception endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /agent/{id}/quest/{quest_id}` | Full quest text, objectives, rewards |
| `GET /agent/{id}/npc/{guid}` | NPC details: vendor items, quests offered, gossip options |
| `GET /agent/{id}/map/{map_id}` | Map name, discovered areas, flight paths known |
| `GET /agent/{id}/spell/{spell_id}` | Spell details: cost, range, cooldown, effects |
| `GET /agent/{id}/item/{entry}` | Item stats, required level, sell price, equip effects |
| `GET /agent/{id}/auction-house` | Current AH listings (if at an auctioneer) |

## Agent Actions (Write API)

Every action is idempotent and validated server-side. Invalid actions return
errors with reasons. Actions are rate-limited per agent (configurable, default
10 actions/second — faster than any human but bounded).

### POST /agent/{id}/action

```json
{
  "action": "move_to",
  "params": {"x": -8910.0, "y": -115.0, "z": 83.3}
}
```

Returns `{"accepted": true, "action_id": "a_0042"}` or error.

### Action catalog

#### Movement
| Action | Params | Description |
|--------|--------|-------------|
| `move_to` | `x, y, z` | Pathfind and walk to position |
| `move_towards` | `guid` | Move towards a unit/object |
| `stop_movement` | — | Halt current movement |
| `face` | `guid` or `x, y` | Face a unit or direction |
| `jump` | — | Jump |
| `use_flight_path` | `node_id` | Take a flight path (must be at flight master) |
| `mount` | `spell_id` | Summon mount |
| `dismount` | — | Dismount |

#### Combat
| Action | Params | Description |
|--------|--------|-------------|
| `cast_spell` | `spell_id, [target_guid]` | Cast a spell |
| `auto_attack` | `guid` | Start auto-attacking target |
| `stop_attack` | — | Stop attacking |
| `set_target` | `guid` | Select target |

#### Interaction
| Action | Params | Description |
|--------|--------|-------------|
| `interact` | `guid` | Open NPC dialog / loot / use object |
| `gossip_select` | `option_index` | Choose a gossip option |
| `accept_quest` | `quest_id` | Accept available quest |
| `turn_in_quest` | `quest_id, [reward_choice]` | Complete quest |
| `abandon_quest` | `quest_id` | Drop a quest |
| `buy_item` | `vendor_guid, entry, [count]` | Buy from vendor |
| `sell_item` | `vendor_guid, bag, slot, [count]` | Sell to vendor |
| `loot` | `guid` | Loot a corpse/object |
| `loot_item` | `guid, slot` | Take specific item from loot window |

#### Inventory
| Action | Params | Description |
|--------|--------|-------------|
| `equip_item` | `bag, slot` | Equip item from inventory |
| `unequip_item` | `slot` | Remove equipped item |
| `use_item` | `bag, slot, [target_guid]` | Use/consume item |
| `delete_item` | `bag, slot, [count]` | Destroy item |
| `split_item` | `bag, slot, count` | Split stack |
| `move_item` | `from_bag, from_slot, to_bag, to_slot` | Rearrange |

#### Social
| Action | Params | Description |
|--------|--------|-------------|
| `say` | `message` | /say in local chat |
| `yell` | `message` | /yell |
| `whisper` | `target_name, message` | Private message |
| `emote` | `emote_id` | Play an emote |
| `invite_to_group` | `guid` | Invite to party |
| `accept_group` | `invite_id` | Accept group invite |
| `leave_group` | — | Leave current group |
| `send_mail` | `recipient, subject, body, [items]` | Send in-game mail |

#### Talents & Skills
| Action | Params | Description |
|--------|--------|-------------|
| `learn_talent` | `talent_id` | Spend talent point |
| `unlearn_talents` | — | Respec (costs gold) |
| `train_spell` | `trainer_guid, spell_id` | Learn from trainer |

## Events (WebSocket)

The agent subscribes to a WebSocket stream for push events, avoiding polling:

```
ws://192.168.1.60:8090/agent/{id}/events
```

Event types:

| Event | Trigger | Payload |
|-------|---------|---------|
| `damage_taken` | Agent receives damage | `{amount, school, attacker_guid, spell_id}` |
| `damage_dealt` | Agent deals damage | `{amount, school, target_guid, spell_id}` |
| `unit_enter_combat` | Unit enters combat near agent | `{unit_guid, target_guid}` |
| `unit_death` | Unit dies near agent | `{unit_guid, killer_guid}` |
| `loot_available` | Corpse becomes lootable | `{corpse_guid}` |
| `quest_progress` | Objective counter changes | `{quest_id, objective_index, count, needed}` |
| `quest_complete` | All objectives met | `{quest_id}` |
| `level_up` | Agent gains a level | `{new_level, stat_gains, new_spells}` |
| `whisper_received` | Incoming whisper | `{sender_name, message}` |
| `group_invite` | Invited to group | `{inviter_guid, inviter_name}` |
| `item_received` | Item enters inventory | `{entry, count, source}` |
| `money_changed` | Gold change | `{new_total, delta, reason}` |
| `death` | Agent dies | `{killer_guid, position}` |
| `resurrect` | Agent resurrects | `{method: "spirit_healer"|"spell"}` |

## Agent Lifecycle

### 1. Spawning

```
POST /agent/spawn
{
  "name": "Agent_Thrallson",
  "race": 2,        // Orc
  "class": 1,       // Warrior
  "account_id": 5,  // Existing WoW account
  "model": "openai/gpt-4o",  // LLM backing the agent (optional)
  "config": {
    "action_rate": 5,        // max actions/sec
    "perception_range": 50,  // yards (default 50)
    "think_interval_ms": 500 // how often to call the LLM (default 500)
  }
}
```

The server creates a character if one doesn't exist at that account/name,
logs it in, and positions it at its hearth location. Returns agent ID.

### 2. Think loop (agent runtime)

```
while agent.alive:
    1. GET /agent/{id}/state          ← full snapshot
    2. GET /agent/{id}/perception     ← what's visible now
    3. LLM(system_prompt + state + perception + memory → next_action)
    4. POST /agent/{id}/action        ← execute one action
    5. wait think_interval_ms
```

The runtime can batch multiple actions before the next perception refresh
(e.g., `move_to` then `interact` when arrival is confirmed by event).

### 3. Despawning

```
POST /agent/{id}/despawn
```

Logs the character out gracefully. The character persists in the database
and can be respawned later.

## Memory Architecture

Agents need persistent memory to function beyond single-session context windows.

### Short-term (LLM context window)
- Last N perception snapshots
- Recent action history
- Current goal stack

### Medium-term (vector store)
- NPCs met: `{name, location, faction, services, notes}`
- Locations visited: `{map, coordinates, description, pois}`
- Quest knowledge: `{quest_id, giver_location, objectives_completed}`
- Combat strategies: `{enemy_type, effective_abilities, pull_range}`

### Long-term (structured DB)
- Completed quests
- Reputation standings
- Known flight paths and routes
- Profession recipes known
- Social graph (other agents/players)

### Memory retrieval

```
GET /agent/{id}/memory/search?q=tauren+quest+giver+barrens&k=5
```

Returns top-k relevant memories from the vector store for injection into the
LLM context.

## Multi-Agent Scenarios

Multiple agents can coexist on the same server, enabling:

### Cooperation
- **Group questing** — agents form parties for elite quests and dungeons
- **Role specialization** — tank/healer/DPS coordination
- **Resource sharing** — trading items, pooling gold
- **Knowledge sharing** — agents broadcast discoveries to a shared memory

### Competition
- **PvP** — battlegrounds and world PvP between agent factions
- **Economy** — auction house competition, resource node racing
- **Limited spawns** — competing for rare mobs and gathering nodes

### Agent communication
Agents communicate through:
1. **In-game channels** — `/say`, `/party`, `/whisper` (human-readable)
2. **Out-of-band bus** — structured messages between agent runtimes for
   coordination that doesn't belong in-character

## Safety & Constraints

Agents operate within the same rules as human players:

- No teleportation (must pathfind)
- No free items or gold
- Level/class restrictions enforced
- PvP flags respected
- Chat profanity filter applies
- Action rate limits prevent server overload

Additional agent-specific constraints:
- **Ethical boundaries** — no griefing, no exploiting, no harassment
- **Session caps** — max concurrent agents configurable
- **Audit log** — every action logged with timestamp, agent ID, and result

## Integration with Existing Server

The agent API runs as an additional service alongside TrinityCore.

### docker-compose addition

```yaml
services:
  agent-api:
    build: ./agent-api
    ports:
      - "8090:8090"
    environment:
      - TRINITY_DB_HOST=trinitycore-db
      - TRINITY_WORLD_HOST=trinitycore-wowserver
      - TRINITY_WORLD_PORT=8085
      - TRINITY_AUTH_HOST=trinitycore-wowserver
      - TRINITY_AUTH_PORT=3724
    depends_on:
      trinitycore-db:
        condition: service_healthy
      trinitycore-wowserver:
        condition: service_started
    volumes:
      - ./agent-api:/app
```

### Implementation approach

Two paths (not mutually exclusive):

**Path A: In-process TrinityCore module (C++)**
- Highest performance, direct memory access
- Requires forking TrinityCore or contributing upstream
- Tight coupling to TrinityCore version

**Path B: Proxy/bot client (Python, recommended for v1)**
- Connects as a WoW client over the standard protocol
- Uses packet parsing to extract state and inject actions
- TrinityCore-agnostic (works with any 3.3.5a server)
- Slower but much simpler to iterate on

Recommended: start with Path B (Python proxy client), then migrate hot paths
to Path A if needed.

## Phase Plan

### Phase 1 — Perception (read-only agent)
- [ ] Python WoW client library (auth + world protocol for 3.3.5a)
- [ ] Character state extraction (position, health, inventory, quests)
- [ ] Entity perception (nearby units, objects, players)
- [ ] REST API serving agent state and perception
- [ ] WebSocket event stream
- [ ] Agent can "observe" the world — no actions yet

### Phase 2 — Basic Actions
- [ ] Movement (pathfinding via mmaps)
- [ ] NPC interaction (gossip, vendor, quest accept/turn-in)
- [ ] Combat (target, auto-attack, spells)
- [ ] Looting
- [ ] Inventory management

### Phase 3 — Agent Runtime
- [ ] Think loop with configurable intervals
- [ ] LLM integration (system prompt + state → action)
- [ ] Action validation and error recovery
- [ ] Memory system (short/medium/long-term)
- [ ] Single agent can autonomously level 1-10

### Phase 4 — Full Autonomy
- [ ] Quest chains and decision-making
- [ ] Talent builds and gear optimization
- [ ] Profession leveling
- [ ] Dungeon navigation (with group)
- [ ] 1-80 leveling capability

### Phase 5 — Multi-Agent
- [ ] Agent spawning/despawning management
- [ ] Group formation and coordination
- [ ] Shared knowledge base
- [ ] PvP scenarios
- [ ] Agent-vs-agent economy simulation

## References

- [TrinityCore](https://trinitycore.org/)
- [TrinityCore 3.3.5 Docs](https://335.trinitycore.net/)
- [WoW Client Protocol (3.3.5a)](https://github.com/TrinityCore/TrinityCore)
- [vmangos — Classic WoW server with bot support](https://github.com/vmangos/core)
- [playerbots — TrinityCore module for AI bots](https://github.com/liyunfan1223/playerbots)