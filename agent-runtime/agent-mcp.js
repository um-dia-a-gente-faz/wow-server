#!/usr/bin/env node
// AI Agent — LLM-driven WoW player via MCP tools
require("dotenv").config({ path: require("path").join(__dirname, ".env") });

const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StdioClientTransport } = require("@modelcontextprotocol/sdk/client/stdio.js");
const { spawn } = require("child_process");

const MODEL = process.env.OPENROUTER_MODEL;
const API_KEY = process.env.OPENROUTER_KEY;
const LLM_URL = process.env.LLM_BASE_URL || "https://openrouter.ai/api/v1";

// ── Agent MCP client ────────────────────────────────────────────────

async function createMCPClient() {
  const transport = new StdioClientTransport({
    command: "node",
    args: [require("path").join(__dirname, "mcp-server.js")],
  });

  const client = new Client(
    { name: "wow-agent", version: "1.0.0" },
    { capabilities: {} }
  );

  await client.connect(transport);
  return client;
}

// ── System prompt for the LLM ───────────────────────────────────────

function buildSystemPrompt(agentName, agentClass, personality) {
  return `You are ${agentName}, a level 1 Blood Elf ${agentClass} playing World of Warcraft.

Your personality: ${personality}

You are in Eversong Woods, the Blood Elf starting zone. You have MCP tools available to interact with the game world. Use them to explore, fight, and interact.

AVAILABLE TOOLS:
- wow_look(agent) — observe your surroundings, see nearby creatures and NPCs
- wow_move(agent, x, y, z) — walk to coordinates
- wow_attack(agent, target_guid?) — attack a target
- wow_say(agent, message) — speak in chat
- wow_target(agent, guid) — target a unit
- wow_loot(agent, guid) — loot a corpse

RULES:
1. ALWAYS call wow_look first to see what's around you
2. Then choose ONE action based on what you see
3. Be curious — explore, talk to NPCs, fight creatures
4. Stay in character — speak like a ${agentClass}

You MUST call a tool on every turn. Do not just describe what you would do — actually DO it.`;
}

// ── LLM call with tool use ──────────────────────────────────────────

async function callLLMWithTools(systemPrompt, messages, tools) {
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
        ...messages,
      ],
      tools: tools.map(t => ({
        type: "function",
        function: {
          name: t.name,
          description: t.description,
          parameters: t.inputSchema,
        },
      })),
      tool_choice: "auto",
      max_tokens: 200,
      temperature: 0.7,
    }),
  });

  const data = await response.json();
  if (data.error) throw new Error(`LLM error: ${data.error.message}`);
  return data.choices[0].message;
}

// ── Main agent loop ─────────────────────────────────────────────────

async function runAgent(agentName, agentClass, personality) {
  console.log(`\n🤖 ${agentName} (${agentClass}) connecting to MCP...`);

  const mcp = await createMCPClient();
  const tools = (await mcp.listTools()).tools.filter(t =>
    ["wow_look", "wow_move", "wow_attack", "wow_say", "wow_target", "wow_loot"].includes(t.name)
  );

  console.log(`[${agentName}] ${tools.length} tools available`);

  const systemPrompt = buildSystemPrompt(agentName, agentClass, personality);
  const messages = [];

  // First turn: force a look
  console.log(`[${agentName}] 🔍 Looking around...`);
  const lookResult = await mcp.callTool({
    name: "wow_look",
    arguments: { agent: agentName },
  });
  const lookText = JSON.parse(lookResult.content[0].text);
  console.log(`[${agentName}]    HP:${lookText.character?.hp} | Near: ${lookText.nearby_units?.length || 0} creatures`);

  messages.push({
    role: "user",
    content: `INITIAL PERCEPTION:\n${JSON.stringify(lookText, null, 2)}\n\nWhat do you do? Call a tool to act.`,
  });

  // Main loop
  for (let turn = 0; turn < 10; turn++) {
    try {
      const llmResponse = await callLLMWithTools(systemPrompt, messages, tools);

      if (llmResponse.tool_calls && llmResponse.tool_calls.length > 0) {
        for (const tc of llmResponse.tool_calls) {
          const toolName = tc.function.name;
          const toolArgs = JSON.parse(tc.function.arguments);

          console.log(`[${agentName}] 🎯 ${toolName}(${JSON.stringify(toolArgs).slice(0, 60)})`);

          const result = await mcp.callTool({
            name: toolName,
            arguments: toolArgs,
          });

          const resultText = result.content[0].text;
          console.log(`[${agentName}]    → ${resultText.slice(0, 100)}`);

          // Add to conversation
          messages.push({
            role: "assistant",
            content: null,
            tool_calls: [tc],
          });
          messages.push({
            role: "tool",
            tool_call_id: tc.id,
            content: resultText,
          });
        }
      } else if (llmResponse.content) {
        console.log(`[${agentName}] 💬 ${llmResponse.content.slice(0, 80)}`);
        messages.push({ role: "assistant", content: llmResponse.content });
        // Nudge to use a tool
        messages.push({ role: "user", content: "Use a tool to act in the game world. Call wow_look or another tool." });
      }

      await new Promise(r => setTimeout(r, 2000));
    } catch (err) {
      console.error(`[${agentName}] Error:`, err.message);
      await new Promise(r => setTimeout(r, 5000));
    }
  }

  console.log(`[${agentName}] Run complete.`);
  await mcp.close();
}

// ── Entry point ─────────────────────────────────────────────────────

const AGENTS = [
  { name: "Silvermoon", cls: "Paladin", personality: "noble protector, formal and honorable" },
  { name: "Farstrider",  cls: "Hunter",  personality: "lone wolf, terse and practical" },
  { name: "Shadowblade", cls: "Rogue",   personality: "sly opportunist, speaks in whispers" },
  { name: "Sunspeaker",  cls: "Priest",  personality: "serene philosopher, speaks of the Light" },
  { name: "Spellweaver", cls: "Mage",    personality: "arrogant intellectual, verbose and dramatic" },
];

const idx = parseInt(process.argv[2]) || 0;
const agent = AGENTS[idx];
if (!agent) {
  console.log("Usage: node agent-mcp.js <0-4>");
  AGENTS.forEach((a, i) => console.log(`  ${i}: ${a.name} (${a.cls})`));
  process.exit(1);
}

runAgent(agent.name, agent.cls, agent.personality).catch(console.error);