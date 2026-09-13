# WoW AI Agent — Development Handoff

**Session date:** 2026-09-13
**Branch:** `spec/ai-agent-gameplay`
**Hermes session:** search for "wow-server agent" in session history

---

## What was built

### Infrastructure
- **WoW Server:** TrinityCore 3.3.5a running on pandora Proxmox VM at `192.168.1.64`
  - World: 8085, Auth: 3724, Web UI: 3000, RA: 3443
  - Docker Compose at `/opt/wow-server/docker-compose.yml` on pandora
  - RA enabled via env override: `TC_WORLD__Ra.Enable=1`
  - MySQL exposed on 3306 (user: `trinity` / pass: `trinity`)
- **FreeLLMAPI:** running on `192.168.1.60:3002` — proxies free LLM models
  - API key: `freellmapi-bde9c92b869fd6b9b456c6b3f42f48f641f98f1ffa8ebeeb`
  - Model `auto` routes to best available free model
  - Models list: `GET /v1/models`
  - Endpoint: `POST /v1/chat/completions` (OpenAI-compatible)

### Accounts and Characters (Blood Elf, Sunstrider Isle, map 530)
| Account ID | Username | Password | Char GUID | Name | Class |
|-----------|----------|----------|-----------|------|-------|
| 1 | GITHUBENS | (user's) | 1 | Rubens | Paladin |
| 2 | AGENT01 | hunter123 | 2 | Silvermoon | Paladin |
| 3 | AGENT02 | hunter123 | 3 | Farstrider | Hunter |
| 4 | AGENT03 | hunter123 | 4 | Shadowblade | Rogue |
| 5 | AGENT04 | hunter123 | 5 | Sunspeaker | Priest |
| 6 | AGENT05 | hunter123 | 6 | Spellweaver | Mage |

All have GM level 3. Health values set per class.

### Agent Runtime (`agent-runtime/`)
```
agent-runtime/
├── .gitignore            # node_modules, .env, *.txt
├── .env                  # Config (NOT committed — contains keys)
├── package.json          # socket.io-client, mysql2, @modelcontextprotocol/sdk
├── db.js                 # MySQL perception layer
├── commander.js          # Socket.IO -> worldserver GM commands
├── wow-bridge.js         # Unified WoW interface for MCP tools
├── mcp-server.js         # MCP server (stdio): 6 agent + 3 observability tools
├── agent.js              # OLD: text-parsing agent (deprecated)
└── agent-mcp.js          # NEW: MCP-based agent with function calling
```

### MCP Tools (registered in Hermes config)

**Agent tools:**
- `wow_look(agent)` — observe surroundings
- `wow_move(agent, x, y, z)` — teleport to coordinates
- `wow_attack(agent, target_guid?)` — attack target
- `wow_say(agent, message)` — speak in local chat
- `wow_target(agent, guid)` — target a unit
- `wow_loot(agent, guid)` — loot a corpse

**Observability tools:**
- `wow_agents_list()` — list all agents with position, HP, state
- `wow_agent_status(agent)` — detailed status with history
- `wow_agent_command(agent, instruction)` — inject admin command

### What works
- MySQL perception: character state, nearby units
- Socket.IO commands: `.announce`, `.target`, `.damage`
- MCP tool calling: agent uses native function calling
- Agent roleplay: Silvermoon greeted Magistrix Erona in-character
- ~50% tool call rate with free models (FreeLLMAPI auto-router)

### Limitations
- Characters are NOT logged in (online=0) — no visual presence in-game
- `.go xyz` doesn't work on offline chars (use MySQL UPDATE instead)
- `.announce` works for system messages
- Needs WoW client protocol (SRP6 + world packets) for real login

---

## How to run

```bash
cd /home/rubens/Repos/wow-server/agent-runtime

# Single agent
node agent-mcp.js 0   # Silvermoon (Paladin)
node agent-mcp.js 1   # Farstrider (Hunter)
node agent-mcp.js 2   # Shadowblade (Rogue)
node agent-mcp.js 3   # Sunspeaker (Priest)
node agent-mcp.js 4   # Spellweaver (Mage)
```

---

## Next steps

### Priority 1: WoW Client Protocol
Implement SRP6 auth + world packets for real login.
- Library: `wow-srp` on PyPI
- Reference: `gtker/wow_messages` Rust crate
- Ports: 3724 (auth) -> 8085 (world)

### Priority 2: Better model
Switch from FreeLLMAPI `auto` to a model with strong function calling.
Add Google API key to FreeLLMAPI dashboard for `gemini-2.5-flash`.

### Priority 3: Multi-agent + Real movement

---

## Useful commands

```bash
# Check server status
nc -zv 192.168.1.64 8085 && nc -zv 192.168.1.64 3724

# MySQL direct access
ssh root@192.168.1.64 'docker exec trinitycore-db mysql -u trinity -ptrinity characters -e "SELECT guid, name, online, position_x, position_y FROM characters;"'

# Restart WoW server
ssh root@192.168.1.64 'docker compose -f /opt/wow-server/docker-compose.yml restart trinitycore-wowserver'

# FreeLLMAPI models
curl -s http://192.168.1.60:3002/v1/models -H "Authorization: Bearer freellmapi-bde9c92b869fd6b9b456c6b3f42f48f641f98f1ffa8ebeeb"

# Move offline character
ssh root@192.168.1.64 'docker exec trinitycore-db mysql -u trinity -ptrinity characters -e "UPDATE characters SET position_x=X, position_y=Y, position_z=Z WHERE guid=N;"'
```