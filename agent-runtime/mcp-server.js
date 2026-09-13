#!/usr/bin/env node
// MCP Server — WoW Agent Tools + Observability
// Communicates via stdio (JSON-RPC per MCP spec)

require("dotenv").config({ path: require("path").join(__dirname, ".env") });
const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require("@modelcontextprotocol/sdk/types.js");

const WoWBridge = require("./wow-bridge");

const bridge = new WoWBridge();

// ── Tool definitions ──────────────────────────────────────────────

const AGENT_TOOLS = [
  {
    name: "wow_look",
    description: "Observe your surroundings. Returns what you see: nearby creatures, objects, NPCs, and your current state (HP, position, quests). Always call this first when entering a new area.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string", description: "Your agent name" },
      },
      required: ["agent"],
    },
  },
  {
    name: "wow_move",
    description: "Move your character to specific coordinates. Use coordinates from wow_look results or known locations.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string", description: "Your agent name" },
        x: { type: "number", description: "X coordinate" },
        y: { type: "number", description: "Y coordinate" },
        z: { type: "number", description: "Z coordinate" },
      },
      required: ["agent", "x", "y", "z"],
    },
  },
  {
    name: "wow_attack",
    description: "Attack a target. Optionally specify a GUID from wow_look results, or attack your current target.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string", description: "Your agent name" },
        target_guid: { type: "integer", description: "GUID of creature to attack (optional)" },
      },
      required: ["agent"],
    },
  },
  {
    name: "wow_say",
    description: "Speak in the local chat channel. Other players and NPCs nearby will hear you.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string", description: "Your agent name" },
        message: { type: "string", description: "What to say" },
      },
      required: ["agent", "message"],
    },
  },
  {
    name: "wow_target",
    description: "Target a specific unit by its GUID.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string" },
        guid: { type: "integer" },
      },
      required: ["agent", "guid"],
    },
  },
  {
    name: "wow_loot",
    description: "Loot a corpse or object by its GUID.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string" },
        guid: { type: "integer" },
      },
      required: ["agent", "guid"],
    },
  },
];

const OBSERVABILITY_TOOLS = [
  {
    name: "wow_agents_list",
    description: "List all running WoW agents and their current state (position, HP, action). For monitoring.",
    inputSchema: { type: "object", properties: {}, required: [] },
  },
  {
    name: "wow_agent_status",
    description: "Get detailed status of a specific agent: full perception, action history, current state.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string", description: "Agent name" },
      },
      required: ["agent"],
    },
  },
  {
    name: "wow_agent_command",
    description: "Inject an instruction/override for an agent. The agent will prioritize this on its next turn.",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string" },
        instruction: { type: "string", description: "What you want the agent to do, e.g. 'Go attack the nearest wolf'" },
      },
      required: ["agent", "instruction"],
    },
  },
];

// ── Tool execution ─────────────────────────────────────────────────

async function handleToolCall(name, args) {
  switch (name) {
    // Agent tools
    case "wow_look":
      return await bridge.look(args.agent);
    case "wow_move":
      return await bridge.move(args.agent, args.x, args.y, args.z);
    case "wow_attack":
      return await bridge.attack(args.agent, args.target_guid || null);
    case "wow_say":
      return await bridge.say(args.agent, args.message);
    case "wow_target":
      return await bridge.target(args.agent, args.guid);
    case "wow_loot":
      return await bridge.loot(args.agent, args.guid);

    // Observability tools
    case "wow_agents_list":
      return bridge.listAgents();
    case "wow_agent_status":
      return bridge.agentStatus(args.agent);
    case "wow_agent_command":
      return bridge.agentCommand(args.agent, args.instruction);

    default:
      return { error: `Unknown tool: ${name}` };
  }
}

// ── Server ─────────────────────────────────────────────────────────

async function main() {
  await bridge.connect();

  // Register agents
  const AGENTS = [
    { name: "Silvermoon", guid: 2, charClass: "Paladin" },
    { name: "Farstrider",  guid: 3, charClass: "Hunter" },
    { name: "Shadowblade", guid: 4, charClass: "Rogue" },
    { name: "Sunspeaker",  guid: 5, charClass: "Priest" },
    { name: "Spellweaver", guid: 6, charClass: "Mage" },
  ];
  for (const a of AGENTS) bridge.registerAgent(a.name, a.guid, a.charClass);

  const server = new Server(
    { name: "wow-agent-mcp", version: "1.0.0" },
    { capabilities: { tools: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [...AGENT_TOOLS, ...OBSERVABILITY_TOOLS],
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    try {
      const result = await handleToolCall(name, args || {});
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: JSON.stringify({ error: err.message }) }],
        isError: true,
      };
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[mcp-server] WoW Agent MCP running on stdio");
  console.error("[mcp-server] Agents:", AGENTS.map(a => a.name).join(", "));
}

main().catch(err => { console.error("Fatal:", err); process.exit(1); });