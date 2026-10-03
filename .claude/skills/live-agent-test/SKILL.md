---
name: live-agent-test
description: Log an agent character into the live TrinityCore server (192.168.1.64) and drive/verify agent actions against it — credentials, which agent characters and NPCs are where, running actions from a script, cross-checking results, and cleanup. Use when a task says to test on the live VM, capture packet fixtures, reproduce agent behaviour in game, or verify an acceptance criterion that needs a real login.
---

# Live agent testing

CI cannot reach the LAN, so anything protocol-level has to be proven here. Read
`docs/AGENT-DIRECTION.md` first; its guardrails win over convenience.

## Guardrails

- No GM commands on agent characters (`.go`, `.die`, `.modify`, …). Relog or walk.
- Never restart the worldserver, never run `scripts/deploy.sh`, never touch accounts.
- The owner's character is **Rubens**. Chat and party invites are fine; nothing else.
- Keep runs short, log out at the end, and leave no agent in a party.
- Never print credentials. Export them, don't echo them.

## Credentials

`AGENT01`–`AGENT05` share `AGENT_PASSWORD`; `LLM_*` settings are in the same file.

```bash
eval "$(ssh root@192.168.1.64 'cd /opt/wow-server; . ./.env; printf "export WOW_HOST=%q WOW_AUTH_PORT=%q WOW_ACCOUNT=AGENT02 WOW_PASSWORD=%q WOW_CHARACTER=Farstrider\n" "$WOW_HOST" "$WOW_AUTH_PORT" "$AGENT_PASSWORD"')"
python3 -m agent --list-chars     # sanity check
python3 -m agent --dry-run        # login, sit 30 s, report perception
```

## Who is where

| Character | Account | Class | Where |
|---|---|---|---|
| Luaprata | AGENT01 | Paladin | Silvermoon City (no mobs nearby) |
| Farstrider | AGENT02 | Hunter | Sunstrider Isle start area |
| Shadowblade | AGENT03 | Rogue | Sunstrider Isle start area |
| Sunspeaker | AGENT04 | Priest | Sunstrider Isle start area |
| Spellweaver | AGENT05 | Mage | Sunstrider Isle start area |

Within ~100 yd of the Sunstrider Isle start area: **Magistrix Erona** (quest giver,
quest 8325 "Reclaiming Sunstrider Isle"), **Shara Sunwing** (vendor, items 9–23 c),
**Ranger Sallina** (hunter trainer), **Pathstalker Kariel** (rogue trainer),
**Springpaw Cub** and **Mana Wyrm** (level 1, attackable) 40–100 yd out.

Two agents can test each other: script one to invite/whisper, run the other with its
LLM think loop, and watch what it decides. That covers the party-companion flow
without needing the owner in game.

## Driving actions from a script

`actions.REGISTRY` holds **instances**, not classes. Movement actions block until they
arrive or fail.

```python
from agent import actions as ac
from agent.auth import auth_logon
from agent.config import load_config
from agent.session import WoWSession

cfg = load_config()
account, key, realms = auth_logon(cfg.wow_host, cfg.wow_auth_port, cfg.account, cfg.password)
r = list(realms.values())[0]
host, port = r["address"].rsplit(":", 1)
sess = WoWSession(host, int(port), account, key, r["id"])
sess.connect()
c = next(c for c in sess.enum_characters() if c["name"].lower() == cfg.character.lower())
sess.race = c["race"]              # needed for the chat language
sess.login_character(c["guid"])
world = sess.world_state
time.sleep(8)                      # let perception and name queries settle

res = ac.REGISTRY["move_towards"].run(sess, world, guid=target_guid, stop_distance=2.0)
print(res.ok, res.error, res.detail)
sess.logout()
```

Useful reads: `world.snapshot(my_position=sess.player_position)`, `world.get_objects()`,
`world.get_my_object()`, `world.get_ui_state()`, `sess.events`, `sess.chat_inbox`.

## Cross-check the result somewhere else

Never trust only the agent's own view:

- `curl -s http://192.168.1.64:9400/api/character/<name>` — the site's DB view
  (characters save every 5 s, so allow a save before comparing).
- The database directly:
  ```bash
  ssh root@192.168.1.64 'cd /opt/wow-server && . ./.env && docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" trinitycore-db \
    mysql -uroot -e "SELECT name, level, money, online FROM characters.characters"'
  ```
- `curl -N http://192.168.1.64:9500/api/chat/stream` — public chat, for chat tests.
- `AGENT_DUMP_PACKETS=<dir>` to capture raw payloads for fixtures.

## Capability probe

`python3 -m agent.tools.probe --help` runs login, chat, move, quest, combat, loot and
rest as one scripted pass (#139). It is **manual-only and counts as a human
intervention** under `docs/AGENT-RUN-1-10.md`: never during a counted run, never
scheduled. Fixtures (`agent/known_targets.py`) are the NPCs and mobs in the table above.

## Known traps

- **Agent characters own no starting gear**, so empty equipment/inventory is correct
  until they loot something. Check the site's API before calling it a bug.
- Fields worth 0 are never sent, so `coinage` is `None` at 0 copper.
- A same-map GM teleport doesn't finish without a movement ack, which is one more
  reason not to teleport agents.
- Dropped-packet warnings in a run are the session's safety net working; check
  `sess.dropped_packets` and the logged reason before assuming a parser bug.
