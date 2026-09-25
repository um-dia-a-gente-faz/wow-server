# Agent direction

Decisions the owner made in a design review on 2026-09-16. They set the goal
for the `agent/` work and override older docs where they disagree
(`docs/AI-AGENT-SPEC.md`, `docs/ROADMAP.md` Phase 3). Linear holds the issues;
this file holds the reasons.

## The goal

AI agents play World of Warcraft on this server and make their own decisions.
The owner logs in with a normal client (character Rubens) and plays alongside
them: sees them walk, fight, quest, chat and group up, and can invite them to
a party.

## Decisions

### 1. Agents are headless protocol clients

Each agent is a Python process (`agent/`) that speaks the 3.3.5a protocol. No
game client, no rendering, no screen reading, no memory reading.

- **Why:** a game client per agent needs a GPU and a window. Headless agents
  cost ~50 MB of RAM each, so 25 of them fit on a small VM.
- **What it means for players:** the server treats an agent like any other
  player. Everything an agent does is broadcast to nearby clients, so Rubens
  sees agents as ordinary players.
- **Rejected:** screenshots + input simulation (slow, unreliable in combat),
  client addons (can't act on their own, limited view), reading `Wow.exe`
  memory (skips parser work, but needs one client per agent).

### 2. Agents decide for themselves

The LLM chooses each agent's actions. Requests from other players, including
Rubens, are input to that decision, not commands. Players influencing an
agent through chat is acceptable; chat is still marked as untrusted text in
the prompt.

Fast, mechanical behaviour runs as **reflexes** between LLM steps: following
the leader and assisting their target (UM-58), and survival (death, rest,
reconnect; UM-43). The LLM turns reflexes on and off; the reflex executes.

### 3. Models are free only

- **Primary:** [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi),
  routing to free models (OpenRouter, OpenCode, and others). Pin a short list
  of models that are good at tool calls; `auto` gave only ~50% valid tool
  calls in the earlier prototype, and UM-44 requires at least 90%.
- **Fallback:** a local model, served OpenAI-compatible (llama.cpp, Ollama or
  vLLM).
- **Later:** paid models may be added; keep the provider behind an interface.
- **One model server per GPU machine**, shared by all agents. Not one model
  per agent.
- **Pacing:** each agent thinks every 10–30 s, not every 3 s.

UM-61 benchmarks the options and sizes the capacity. Estimates until then:

| Machine | GPU | Fits | Role |
|---|---|---|---|
| Laptop: Core Ultra 7, 64 GB | RTX 4060-class, 8 GB | 7–8B models; roughly 10–20 agents | Main local model server during play sessions (not 24/7; watch thermals) |
| Desktop: i5-14600KF, 32 GB | RTX 4070 SUPER, 12 GB | up to ~14B | Extra capacity; competes with the owner's WoW client |
| Pandora: Ryzen 5 5600H, 32 GB, Proxmox | RTX 3050 Ti, 4 GB | 3–4B only | Hosts the agent processes (a separate small VM, not the 6 GB wow-server VM) |

### 4. Communication happens in game only

Agents coordinate through whispers, say, party and guild chat, and invites.
No hidden channel, no shared state between agent processes. This keeps
behaviour organic, like people talking.

**Anti-loop rules** (so two agents can't chat forever and burn requests):

1. Incoming chat never triggers an extra LLM call. It's added to the next
   regular think step, so cost stays bounded however much agents talk.
2. Staying silent is always a valid choice, and the prompt says so: the agent
   is playing, not chatting; real players don't answer every message.
3. Code limits: at most one outgoing message per agent per N seconds; a cap of
   about 6 consecutive exchanges with the same player, then a cooldown; no
   repeating identical or near-identical messages.
4. Only messages addressed to the agent (whisper, party, guild, name mention)
   are marked as such; general chat is background.

Also: the server echoes an agent's own messages back, so agents ignore chat
whose sender is themselves.

### 5. Memory is private

Each agent may keep its own memory outside the game (places, quests, players
met). No memory is shared between agents.

### 6. Scale and roster

- 5 agents for a party, 25 for a raid and a guild, more if hardware allows.
- All characters are Horde, with random races and classes (UM-63).

## Milestones

| Milestone | Done when | Issues |
|---|---|---|
| **1 — Party companion** | Rubens invites an agent; it decides to accept and says so in party chat, follows him, attacks what he attacks, and answers when talked to | UM-31–39, UM-44, UM-58, UM-61, UM-64 |
| **2 — Human-like player** | Agents quest, loot, sell, trade, mail gold, survive deaths, and level 1→10 unattended | UM-40–43, UM-55, UM-59, UM-60 |
| **3 — Jev decision brain** | Jev (not the LLM) decides tactical actions — combat, loot, quests, movement, party-accept/follow/assist — for 1 agent (1→10), then a party of 5, then a 25-agent raid roster. See `docs/adr/0001-jev-in-the-think-loop.md`. | UM-63, UM-95–UM-102 |

Agent-initiated social grouping (agents noticing each other and forming
parties through chat, the milestone's original scope) is deferred to
"Later — autonomy & multi-agent" (UM-62) — see the ADR above for why.

The first milestone replaced the roadmap's original order (one agent levels
1→10 before any multi-agent work) because playing together is the point.

## Known findings that shape the work

- **`tools/chat-feed` can't read chat on this server build.** TrinityCore here
  never writes player chat to any log, so tailing `Server.log` shows nothing.
  The chat panel (UM-47) and hardening (UM-48) need a different data source
  first, for example a listener agent relaying `SMSG_MESSAGECHAT`.
- **Moving objects drop packets — FIXED by UM-64.** The update-object parser
  now handles spline movement (the create-object spline block,
  `SMSG_MONSTER_MOVE`, and `MSG_MOVE_*` broadcasts), so a packet containing a
  walking NPC or player parses cleanly instead of being dropped whole.
- **Teleport ack is missing.** Without `MSG_MOVE_TELEPORT_ACK` a same-map GM
  teleport doesn't finish server-side (UM-38). Don't teleport agent characters
  to set up tests; relog or walk instead.
- **Stacked PRs can merge "successfully" without ever reaching `main` — FIXED
  by re-opening against `main` directly, but the branching rule changed
  because of it.** On 2026-09-17, UM-38 and UM-39 were each opened as a PR
  based on the previous ticket's still-open branch (the convention at the
  time, meant to keep a real dependency visible). Both got merged — but
  *into their base branch*, not into `main`, because that base branch had
  already been separately squash-merged into `main` moments earlier
  (squash-merge creates a new commit, severing the ancestry link back to
  the original branch). Linear and GitHub both showed the tickets as done;
  `main` silently had neither `agent/movement.py`'s v1 actions nor
  `agent/spells.py` at all. Caught only because a later PR stacked on top
  of the same chain started showing merge conflicts against `main`.
  Recovered by cherry-picking each ticket's own commit onto current `main`
  and re-opening PRs #32/#33 there. **New rule, effective immediately: every
  PR's base is `main`, never another branch — see `CONTRIBUTING.md`
  → Branches.**
