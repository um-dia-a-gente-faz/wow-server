// Database layer — queries TrinityCore MySQL for agent perception
const mysql = require("mysql2/promise");

class PerceptionDB {
  constructor() {
    this.charsPool = null;
    this.worldPool = null;
  }

  async connect() {
    const base = {
      host: process.env.MYSQL_HOST,
      user: process.env.MYSQL_USER,
      password: process.env.MYSQL_PASS,
    };
    this.charsPool = mysql.createPool({ ...base, database: process.env.MYSQL_DB_CHARS });
    this.worldPool = mysql.createPool({ ...base, database: process.env.MYSQL_DB_WORLD });
  }

  async getCharacter(guid) {
    const [rows] = await this.charsPool.execute(
      `SELECT guid, account, name, race, class, gender, level, xp, money,
              position_x, position_y, position_z, map, zone, orientation,
              health, power1 as mana, power3 as energy, online
       FROM characters WHERE guid = ?`, [guid]
    );
    return rows[0] || null;
  }

  async getNearbyUnits(map, x, y, z, range = 50) {
    const [rows] = await this.worldPool.execute(
      `SELECT c.guid, c.id as entry, ct.name, c.position_x, c.position_y, c.position_z, c.map,
              SQRT(POW(c.position_x - ?, 2) + POW(c.position_y - ?, 2) + POW(c.position_z - ?, 2)) as distance
       FROM creature c
       JOIN creature_template ct ON c.id = ct.entry
       WHERE c.map = ?
         AND SQRT(POW(c.position_x - ?, 2) + POW(c.position_y - ?, 2) + POW(c.position_z - ?, 2)) < ?
       ORDER BY distance LIMIT 20`,
      [x, y, z, map, x, y, z, range]
    );
    return rows;
  }

  async getNearbyObjects(map, x, y, z, range = 50) {
    const [rows] = await this.worldPool.execute(
      `SELECT g.guid, g.id as entry, gt.name, g.position_x, g.position_y, g.position_z, g.map,
              SQRT(POW(g.position_x - ?, 2) + POW(g.position_y - ?, 2) + POW(g.position_z - ?, 2)) as distance
       FROM gameobject g
       JOIN gameobject_template gt ON g.id = gt.entry
       WHERE g.map = ?
         AND SQRT(POW(g.position_x - ?, 2) + POW(g.position_y - ?, 2) + POW(g.position_z - ?, 2)) < ?
       ORDER BY distance LIMIT 20`,
      [x, y, z, map, x, y, z, range]
    );
    return rows;
  }

  async getQuestLog(guid) {
    const [rows] = await this.charsPool.execute(
      `SELECT q.quest as quest_id, qt.LogTitle as title, q.status
       FROM character_queststatus q
       JOIN world.quest_template qt ON q.quest = qt.ID
       WHERE q.guid = ? AND q.status IN (1, 2)`,
      [guid]
    );
    return rows;
  }

  async getInventory(guid) {
    const [rows] = await this.charsPool.execute(
      `SELECT ci.item as entry, ci.count, it.name
       FROM character_inventory ci
       JOIN item_instance ii ON ci.item = ii.guid
       JOIN world.item_template it ON ii.itemEntry = it.entry
       WHERE ci.guid = ?`,
      [guid]
    );
    return rows;
  }

  async close() {
    if (this.charsPool) await this.charsPool.end();
    if (this.worldPool) await this.worldPool.end();
  }
}

module.exports = PerceptionDB;