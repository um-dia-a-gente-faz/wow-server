// WoW Bridge — connects MCP tools to the WoW server
// Uses MySQL for perception, Socket.IO for GM commands
require("dotenv").config();
const PerceptionDB = require("./db");
const io = require("socket.io-client");

class WoWBridge {
  constructor() {
    this.db = null;
    this.socket = null;
    this.connected = false;
    this.agents = new Map(); // agentName -> { guid, perception, state, history }
  }

  async connect() {
    this.db = new PerceptionDB();
    await this.db.connect();

    return new Promise((resolve, reject) => {
      const url = `http://${process.env.WOW_HOST}:${process.env.WOW_WEB_PORT}`;
      this.socket = io(url, { transports: ["websocket", "polling"] });
      this.socket.on("connect", () => {
        this.connected = true;
        resolve();
      });
      this.socket.on("connect_error", (err) => reject(err));
      setTimeout(() => reject(new Error("Socket.IO timeout")), 5000);
    });
  }

  sendCommand(cmd) {
    if (this.connected) this.socket.emit("worldserver_input", cmd);
  }

  // ── Agent registration ──────────────────────────────────────────
  registerAgent(name, guid, charClass) {
    this.agents.set(name, {
      guid, charClass, name,
      state: "idle",
      lastAction: null,
      lastThought: null,
      history: [],
      perception: null,
    });
  }

  // ── Agent TOOLS ──────────────────────────────────────────────────

  async look(agentName) {
    const agent = this.agents.get(agentName);
    if (!agent) return { error: `Agent ${agentName} not registered` };

    const char = await this.db.getCharacter(agent.guid);
    if (!char) return { error: "Character not found" };

    const units = await this.db.getNearbyUnits(char.map, char.position_x, char.position_y, char.position_z);
    const objects = await this.db.getNearbyObjects(char.map, char.position_x, char.position_y, char.position_z);
    const quests = await this.db.getQuestLog(agent.guid);

    const perception = {
      character: {
        name: char.name, class: agent.charClass, level: char.level,
        hp: char.health, xp: char.xp, money: char.money,
        position: { map: char.map, x: char.position_x, y: char.position_y, z: char.position_z },
      },
      nearby_units: units.map(u => ({
        guid: u.guid, name: u.name, entry: u.entry,
        distance_yards: Math.round(u.distance * 10) / 10,
      })),
      nearby_objects: objects.slice(0, 5).map(o => ({
        guid: o.guid, name: o.name,
        distance_yards: Math.round(o.distance * 10) / 10,
      })),
      quests: quests.map(q => ({
        id: q.quest_id, title: q.title, status: q.status === 1 ? "in_progress" : "complete",
      })),
    };

    agent.perception = perception;
    agent.state = "observing";
    this._logHistory(agent, "LOOK", perception);
    return perception;
  }

  async move(agentName, x, y, z) {
    const agent = this.agents.get(agentName);
    if (!agent) return { error: `Agent ${agentName} not registered` };

    this.sendCommand(`.go xyz ${x} ${y} ${z} 530`);
    agent.state = "moving";
    agent.lastAction = { type: "MOVE", x, y, z };
    this._logHistory(agent, "MOVE", { x, y, z });

    // Wait briefly and check new position
    await new Promise(r => setTimeout(r, 1500));
    const char = await this.db.getCharacter(agent.guid);
    return {
      moved_to: { x: char.position_x, y: char.position_y, z: char.position_z },
    };
  }

  async attack(agentName, targetGuid) {
    const agent = this.agents.get(agentName);
    if (!agent) return { error: `Agent ${agentName} not registered` };

    if (targetGuid) this.sendCommand(`.target guid ${targetGuid}`);
    this.sendCommand(`.damage 10`);
    agent.state = "fighting";
    agent.lastAction = { type: "ATTACK", target: targetGuid };
    this._logHistory(agent, "ATTACK", { target: targetGuid });
    return { action: "attacking", target: targetGuid || "current target" };
  }

  async say(agentName, message) {
    const agent = this.agents.get(agentName);
    if (!agent) return { error: `Agent ${agentName} not registered` };

    this.sendCommand(`.announce ${agentName}: ${message}`);
    this._logHistory(agent, "SAY", { message });
    return { said: message };
  }

  async target(agentName, guid) {
    this.sendCommand(`.target guid ${guid}`);
    const agent = this.agents.get(agentName);
    this._logHistory(agent, "TARGET", { guid });
    return { targeted: guid };
  }

  async loot(agentName, guid) {
    this.sendCommand(`.loot ${guid}`);
    const agent = this.agents.get(agentName);
    this._logHistory(agent, "LOOT", { guid });
    return { looting: guid };
  }

  // ── OBSERVABILITY TOOLS ──────────────────────────────────────────

  listAgents() {
    const list = [];
    for (const [name, agent] of this.agents) {
      list.push({
        name,
        class: agent.charClass,
        state: agent.state,
        last_action: agent.lastAction,
        position: agent.perception?.character?.position || null,
        hp: agent.perception?.character?.hp || null,
        level: agent.perception?.character?.level || null,
      });
    }
    return { agents: list, count: list.length };
  }

  agentStatus(agentName) {
    const agent = this.agents.get(agentName);
    if (!agent) return { error: `Agent ${agentName} not found` };

    return {
      name: agent.name,
      class: agent.charClass,
      state: agent.state,
      last_action: agent.lastAction,
      last_thought: agent.lastThought,
      perception: agent.perception,
      history: agent.history.slice(-10),
    };
  }

  agentCommand(agentName, instruction) {
    const agent = this.agents.get(agentName);
    if (!agent) return { error: `Agent ${agentName} not found` };

    agent.lastThought = `[OVERRIDE] Admin command: ${instruction}`;
    agent.state = "awaiting_override";
    return { injected: instruction, agent: agentName };
  }

  // ── Helpers ──────────────────────────────────────────────────────

  _logHistory(agent, action, data) {
    agent.history.push({
      time: new Date().toISOString(),
      action,
      data: JSON.stringify(data).slice(0, 200),
    });
    if (agent.history.length > 50) agent.history.shift();
  }

  close() {
    if (this.socket) this.socket.close();
    if (this.db) this.db.close();
  }
}

module.exports = WoWBridge;