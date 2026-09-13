// AI Agent — LLM-driven WoW player
require("dotenv").config();
const PerceptionDB = require("./db");
const Commander = require("./commander");

const AGENT_CONFIGS = [
  { guid: 2, name: "Silvermoon", charClass: "Paladin", personality: "noble protector, formal speech" },
  { guid: 3, name: "Farstrider",  charClass: "Hunter",  personality: "lone wolf, terse, practical" },
  { guid: 4, name: "Shadowblade", charClass: "Rogue",   personality: "sly, opportunistic, whispers" },
  { guid: 5, name: "Sunspeaker",  charClass: "Priest",  personality: "serene, philosophical, healing" },
  { guid: 6, name: "Spellweaver", charClass: "Mage",    personality: "arrogant intellectual, verbose" },
];

const MODEL = process.env.OPENROUTER_MODEL;
const API_KEY = process.env.OPENROUTER_KEY;
const LLM_URL = process.env.LLM_BASE_URL || "https://openrouter.ai/api/v1";

// ── System prompt for the LLM agent ──────────────────────────────────
function buildSystemPrompt(agent) {
  return `WoW Bot — ${agent.name}, Level 1 Blood Elf ${agent.charClass}. ${agent.personality}.

OUTPUT FORMAT: One line. One of these exact words:
  LOOK
  MOVE <x> <y> <z>
  SAY <text>
  TARGET <guid>
  ATTACK
  LOOT <guid>
  REST

No other text allowed. No explanations.`;
}

// ── Build perception context for the LLM ──────────────────────────────
async function buildPerceptionContext(db, agent) {
  const char = await db.getCharacter(agent.guid);
  if (!char) return { error: "Character not found" };

  const units = await db.getNearbyUnits(char.map, char.position_x, char.position_y, char.position_z);
  const objects = await db.getNearbyObjects(char.map, char.position_x, char.position_y, char.position_z);
  const quests = await db.getQuestLog(agent.guid);

  const nearby = units.map(u =>
    `  [${u.guid}] ${u.name} (lvl ${u.entry > 100000 ? "?" : ""}) at ${u.distance.toFixed(1)}y`
  ).join("\n");

  const nearbyObjects = objects.slice(0, 5).map(o =>
    `  [${o.guid}] ${o.name} at ${o.distance.toFixed(1)}y`
  ).join("\n");

  const questInfo = quests.length > 0
    ? quests.map(q => `  #${q.quest_id} ${q.title} [${q.status === 1 ? "IN PROGRESS" : "COMPLETE"}]`).join("\n")
    : "  (none)";

  return `=== YOUR STATE ===
Name: ${char.name} | ${agent.charClass} | Level ${char.level}
HP: ${char.health} | XP: ${char.xp}
Position: map=${char.map} x=${char.position_x.toFixed(1)} y=${char.position_y.toFixed(1)} z=${char.position_z.toFixed(1)}

=== NEARBY CREATURES ===
${nearby || "  (none nearby)"}

=== NEARBY OBJECTS ===
${nearbyObjects || "  (none nearby)"}

=== QUESTS ===
${questInfo}

What do you do? (respond with ONE action)`;
}

// ── Call OpenRouter LLM ──────────────────────────────────────────────
async function callLLM(systemPrompt, userMessage) {
  const response = await fetch(`${LLM_URL}/chat/completions`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userMessage },
      ],
      max_tokens: 30,
      temperature: 0.3,
    }),
  });

  const data = await response.json();
  if (data.error) throw new Error(`LLM error: ${data.error.message}`);
  let action = data.choices[0].message.content.trim();
  // Take only the first line
  action = action.split('\n')[0].trim();
  // Remove common prefixes
  action = action.replace(/^(Action:|The action is:|I choose:|I will|We need to|We should|We must|We can|We are|The user|Let me|I am)\s*/i, '').trim();
  // If it's still a sentence, try to extract just the action keyword
  const actionMatch = action.match(/^(LOOK|MOVE|SAY|TARGET|ATTACK|LOOT|REST)\b/i);
  if (actionMatch) {
    action = action.slice(actionMatch.index);
  }
  // Take first word as fallback
  if (!/^(LOOK|MOVE|SAY|TARGET|ATTACK|LOOT|REST)\b/i.test(action)) {
    // Try finding any action keyword in the string
    const found = action.match(/\b(LOOK|MOVE|SAY|TARGET|ATTACK|LOOT|REST)\b/i);
    action = found ? found[0].toUpperCase() : action.split(/\s+/)[0].toUpperCase();
  }
  return action;
}

// ── Parse and execute LLM action ──────────────────────────────────────
function executeAction(action, cmd, agent) {
  const parts = action.split(/\s+/);
  const verb = parts[0].toUpperCase();

  switch (verb) {
    case "LOOK":
      cmd.send(".look"); // does nothing but signals intent to observe
      return "observing surroundings";

    case "MOVE":
    case "GO": {
      const [_, x, y, z] = parts.map(Number);
      if (isNaN(x) || isNaN(y) || isNaN(z)) return "invalid MOVE coordinates";
      cmd.goXYZ(agent.guid, 530, x, y, z);
      return `moving to ${x}, ${y}, ${z}`;
    }

    case "SAY":
    case "YELL": {
      const msg = action.slice(verb.length).trim();
      cmd.say(msg);
      return `saying: "${msg}"`;
    }

    case "TARGET": {
      const guid = parseInt(parts[1]);
      if (isNaN(guid)) return "invalid target GUID";
      cmd.send(`.target guid ${guid}`);
      return `targeting ${guid}`;
    }

    case "ATTACK":
      cmd.damage(10); // GM damage command to simulate auto-attack
      return "attacking!";

    case "LOOT": {
      const guid = parseInt(parts[1]);
      if (isNaN(guid)) return "invalid loot GUID";
      cmd.send(`.loot ${guid}`);
      return `looting ${guid}`;
    }

    case "QUEST": {
      const sub = parts[1]?.toUpperCase();
      const questId = parseInt(parts[2]);
      if (sub === "ACCEPT") {
        cmd.addQuest(questId);
        return `accepting quest #${questId}`;
      } else if (sub === "TURNIN") {
        cmd.completeQuest(questId);
        return `completing quest #${questId}`;
      }
      return "unknown QUEST sub-action";
    }

    case "REST":
      return "resting...";

    case "LEARN": {
      const spellId = parseInt(parts[1]);
      if (isNaN(spellId)) return "invalid spell ID";
      cmd.learnSpell(spellId);
      return `learning spell #${spellId}`;
    }

    case "EQUIP": {
      const entry = parseInt(parts[1]);
      if (isNaN(entry)) return "invalid item entry";
      cmd.send(`.equip ${entry}`);
      return `equipping item #${entry}`;
    }

    default:
      return `unknown action: ${verb}`;
  }
}

// ── Main agent loop ───────────────────────────────────────────────────
async function runAgent(agentConfig) {
  const db = new PerceptionDB();
  const cmd = new Commander();

  await db.connect();
  await cmd.connect();

  // Wait for worldserver to be ready
  await new Promise(r => setTimeout(r, 2000));

  console.log(`\n🤖 ${agentConfig.name} (${agentConfig.charClass}) is ONLINE`);
  console.log("═".repeat(50));

  const systemPrompt = buildSystemPrompt(agentConfig);

  for (let turn = 0; turn < 3; turn++) {
    try {
      // 1. Perceive
      const perception = await buildPerceptionContext(db, agentConfig);
      if (perception.error) {
        console.log(`[${agentConfig.name}] ${perception.error} — waiting...`);
        await new Promise(r => setTimeout(r, 3000));
        continue;
      }

      // 2. Think (LLM)
      const action = await callLLM(systemPrompt, perception);
      console.log(`[${agentConfig.name}] 🧠 ${action}`);

      // 3. Act
      const result = executeAction(action, cmd, agentConfig);
      console.log(`[${agentConfig.name}] ⚔️  ${result}`);

      // 4. Wait before next turn
      await new Promise(r => setTimeout(r, 3000));
    } catch (err) {
      console.error(`[${agentConfig.name}] Error:`, err.message);
      await new Promise(r => setTimeout(r, 5000));
    }
  }

  console.log(`[${agentConfig.name}] Run complete.`);
  cmd.close();
  await db.close();
}

// ── Entry point ──────────────────────────────────────────────────────
const agentIndex = parseInt(process.argv[2]) || 0;
const agent = AGENT_CONFIGS[agentIndex];

if (!agent) {
  console.log("Usage: node agent.js <index 0-4>");
  console.log("Agents:");
  AGENT_CONFIGS.forEach((a, i) => console.log(`  ${i}: ${a.name} (${a.charClass})`));
  process.exit(1);
}

if (!API_KEY || API_KEY.includes("your-key")) {
  console.error("ERROR: Set OPENROUTER_KEY in .env file");
  process.exit(1);
}

runAgent(agent).catch(console.error);