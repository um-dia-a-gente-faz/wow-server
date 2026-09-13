// Commander — sends GM commands to worldserver via Socket.IO
const io = require("socket.io-client");

class Commander {
  constructor() {
    this.socket = null;
    this.connected = false;
  }

  async connect() {
    return new Promise((resolve, reject) => {
      const url = `http://${process.env.WOW_HOST}:${process.env.WOW_WEB_PORT}`;
      this.socket = io(url, {
        transports: ["websocket", "polling"],
        reconnection: true,
      });

      this.socket.on("connect", () => {
        this.connected = true;
        console.log("[commander] Connected to worldserver console");
        resolve();
      });

      this.socket.on("connect_error", (err) => {
        reject(new Error(`Commander connection failed: ${err.message}`));
      });

      this.socket.on("disconnect", () => {
        this.connected = false;
      });

      setTimeout(() => reject(new Error("Commander connection timeout")), 5000);
    });
  }

  // Send a GM command to the worldserver
  send(command) {
    if (!this.connected) {
      console.warn("[commander] Not connected, dropping command:", command);
      return;
    }
    this.socket.emit("worldserver_input", command);
  }

  // Move character to coordinates
  goXYZ(guid, map, x, y, z) {
    this.send(`.go xyz ${x} ${y} ${z} ${map}`);
  }

  // Target nearest creature
  targetNearest() {
    this.send(".target nearest creature");
  }

  // Add quest
  addQuest(questId) {
    this.send(`.quest add ${questId}`);
  }

  // Complete quest
  completeQuest(questId) {
    this.send(`.quest complete ${questId}`);
  }

  // Learn spell
  learnSpell(spellId) {
    this.send(`.learn ${spellId}`);
  }

  // Add item
  addItem(itemId, count = 1) {
    this.send(`.additem ${itemId} ${count}`);
  }

  // Set level
  setLevel(level) {
    this.send(`.level ${level}`);
  }

  // Modify money (copper)
  modifyMoney(amount) {
    this.send(`.modify money ${amount}`);
  }

  // Say something in chat
  say(message) {
    this.send(`.announce ${message}`);
  }

  // Cast spell
  castSpell(spellId) {
    this.send(`.cast ${spellId}`);
  }

  // Damage target
  damage(amount) {
    this.send(`.damage ${amount}`);
  }

  close() {
    if (this.socket) this.socket.close();
  }
}

module.exports = Commander;