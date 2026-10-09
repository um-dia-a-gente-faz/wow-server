# Max health / max power spike

> **Archived spike.** Outcome: no-go on RA/GM commands, go on a config-only alternative. Issue: UM-46. Superseded by: nothing.

This note investigates ROADMAP "Operator dashboard panel", Phase B option 2:
can the RA console (`:3443`) or GM commands (via `scripts/wow_console.py`)
report **max** health and power for an arbitrary online character, so the
wowmap inspect drawer can draw bars? (UM-46.)

**Verdict: no-go on RA/GM commands, go on a config-only alternative.** No
stock command usable from a console prints a player's max health or power.
TrinityCore can already persist exactly those values to
`characters.character_stats` on every player save. That table exists on the
live realm but is empty because the feature is off by default. Turning it on
takes two worldserver config keys and a `LEFT JOIN` in wowmap. No C++ or new
service is needed.

Checked against TrinityCore branch `3.3.5` at `2ac2d9055061`, the exact
revision the live worldserver reports (`server info` below), and against the
live realm on 192.168.1.64 with read-only commands only.

## 1. Which commands print max health / power?

A grep of `src/server/scripts/Commands/` for `GetMaxHealth`/`GetMaxPower`
finds exactly one hit:

```text
cs_npc.cpp:494  handler->PSendSysMessage(LANG_NPCINFO_HEALTH, target->GetCreateHealth(), target->GetMaxHealth(), target->GetHealth());
```

That is `.npc info`. It uses `getSelectedCreature()`, so it only works on
creatures, and it is registered `Console::No`.

The other candidates from the card:

| Command | Source | Max HP/power? | From console? |
|---|---|---|---|
| `.pinfo <name>` | `cs_misc.cpp` `HandlePInfoCommand` | No: account, level/XP, race/class, alive, money, map/zone, guild, played time | Yes, by name, online or offline |
| `.npc info` | `cs_npc.cpp` | Yes, creatures only | `Console::No` |
| `.debug getvalue <index>` | `cs_debug.cpp` `HandleDebugGetValueCommand` | Raw update field, e.g. index 32 = `UNIT_FIELD_MAXHEALTH` | `Console::No`, needs `getSelectedUnit()` |
| `.lookup player account/ip/email` | `cs_lookup.cpp` | No: character names and GUIDs only | Yes |
| `.character ...` | `cs_character.cpp` | No: rename/level/reputation/deleted/erase; several modify state | Mixed |
| `.debug *` on console | `cs_debug.cpp` | Only `arena`, `bg`, `guidlimits`, `loadcells`, `objectcount`, `questreset`, `warden`, `asan` are `Console::Yes`; none read unit fields | n/a |

`.debug getvalue` is the only path to a player's max health. From the
console it can't work: `ChatHandler::getSelectedUnit()` returns `nullptr`
when there is no `WorldSession` (`game/Chat/Chat.cpp:206`), and the command
table hides it from console callers anyway. In game, it needs a GM character
that has the player *targeted*, which means being in visibility range. That
is the same limitation as agent perception (option 4), with more steps.

Field indices, from `Entities/Object/Updates/UpdateFields.h`:
`OBJECT_END = 0x06`, `UNIT_FIELD_HEALTH = 0x18` (24), `UNIT_FIELD_POWER1..7
= 25..31`, `UNIT_FIELD_MAXHEALTH = 0x20` (32), `UNIT_FIELD_MAXPOWER1..7 =
33..39`.

### Live transcript (RA, account `AGENT01`, password redacted)

```text
Authentication Required
Username: AGENT01
Password: ********
Welcome to a Trinity Core server.
TC>server info
TrinityCore rev. 2ac2d9055061 2026-05-29 19:34:34 +0200 (3.3.5 branch) (Linux, x86_64, RelWithDebInfo, Static)
Online players: 0 (max: 1)
Active connections: 0 (max: 1) Queued connections: 0 (max: 0)
Server uptime: 1 Day 2 Hours 49 Minutes 50 Seconds.
Update time diff: 1.
TC>pinfo Rubens
│Player  (offline) Rubens (GUID Full: 0x0000000000000001 Type: Player Low: 1)
│ Account: GITHUBENS (ID: 1), GMLevel: 3
│ Last Login: 2026-09-15 22:18:06 (Failed Logins: 0)
│ OS: Win - Latency: 0 ms
└ Registration Email:  - Email:
│ Last IP: 192.168.1.221 (Locked: No)
│ Level: 2 (170/900 XP (730 XP left))
│ Race: Male Blood Elf, Paladin
│ Alive ?: Yes
│ Money: 0g0s30c
│ Map: Outland, Zone: Eversong Woods
│ Played time: 5h
TC>debug getvalue 32 1
### USAGE: .debug ...
Possible subcommands:
|- debug arena
|- debug asan ...
|- debug bg
|- debug guidlimits
|- debug loadcells
|- debug objectcount
|- debug questreset
|- debug warden ...
TC>npc info
Command 'npc info' does not exist
TC>lookup player account AGENT01
Characters at account AGENT01 (Id: 2)
  Luaprata (GUID 2)
TC>quit
Bye
```

`scripts/wow_console.py 'pinfo Luaprata'` returns the same `pinfo` layout
through the web UI's socket.io route, since both routes run commands through
the same CLI handler.

No character was online during the spike (`Online players: 0`). `pinfo`
takes its fields from the live `Player*` when the character is online, but
that path still never reads health or power (`cs_misc.cpp`, the `if
(target)` branch), so an online test would not change the answer. Logging a
character in just to test was out of scope for a read-only spike.

## 2. RA: auth flow and parseability

It works without a TTY. The flow (`worldserver/RemoteAccess/RASession.cpp`):

1. Connect over TCP. Optional telnet negotiation bytes are drained if present;
   a raw socket needs none.
2. Server sends `Authentication Required\r\nUsername: `. Client sends
   `<user>\r\n`.
3. Server sends `Password: `. Client sends `<password>\r\n`.
4. The server checks two things. `CheckAccessLevel` requires
   `account_access.SecurityLevel >= Ra.MinLevel` (live: 3) **and**
   `RealmID = -1`. `CheckPassword` verifies the upper-cased user/password
   against the SRP6 salt/verifier. On failure it sends
   `Authentication failed\r\n` and closes the socket.
5. On success it sends the MOTD line(s), then `TC>`. Each `<command>\r\n` is
   queued into the world thread (`sWorld->QueueCliCommand`). The session
   blocks until it finishes, streams the output, and sends `TC>` again.
   `quit`/`exit`/`logout` → `Bye`. An empty line closes the session.

On the live realm, `AGENT01`–`AGENT05` and `GITHUBENS` are all
SecurityLevel 3, RealmID -1, so no new account or RBAC change is needed for
RA. There is no framing beyond the `TC>` prompt: read until the buffer ends
in `TC>`. Output is human-formatted text, and some of it has UTF-8 box-drawing
characters (`│`, `└`, as in `pinfo`), so any consumer needs per-command
regexes. That is workable for `pinfo`, but it has no value here
because no command carries the numbers.

## 3. Target vs. name

- `.pinfo` takes a name (`Optional<PlayerIdentifier>`, falling back to
  target/self), but has no health/power.
- `.debug getvalue` and `.npc info` take **only** the current in-game
  selection. No console form exists, so they can't be driven from RA or
  `wow_console.py`.

## 4. Cost of polling

Measured from the workstation to 192.168.1.64 over RA with one persistent
session:

| Step | Latency |
|---|---|
| TCP connect + auth (SRP6 check + 2 login-DB queries) | ~1,050 ms |
| Each command (`server info`, `pinfo`, `lookup`, `gm list`) | 45–47 ms |

Commands are serialized through the world update loop, so a poller has to
keep one authenticated session open and send commands one after another.
Reconnecting per poll costs about 1 s of auth each time. At 45 ms per
character, 5 s polling would handle roughly 100 online characters in theory.
It's moot for health/power, though, since no command returns the values.

Log noise is not a concern either way. RA logs to the `commands.ra`
category at INFO, but the live config has no `Logger.commands.ra`, so it
falls back to `Logger.root=5` (Error only). After the spike, `Server.log`
contained 0 RA lines and `docker logs` showed no new output. The chat-feed
tailer would not see RA traffic.

## 5. Alternatives, and the one that works

### (d) `character_stats`: recommended

`Player::_SaveStats` (`Entities/Player/Player.cpp:19915`, comment: "save
player stats -- only for external usage") runs inside `Player::SaveToDB`.
It `DELETE`s and re-`INSERT`s one row in `characters.character_stats`:

```text
guid, maxhealth, maxpower1..maxpower7, strength, agility, stamina, intellect, spirit,
armor, resHoly, resFire, resNature, resFrost, resShadow, resArcane,
blockPct, dodgePct, parryPct, critPct, rangedCritPct, spellCritPct,
attackPower, rangedAttackPower, spellPower, resilience
```

`maxhealth` is `GetMaxHealth()` and `maxpowerN` is `GetMaxPower(Powers(N-1))`,
the same runtime values the client sees. Two settings gate it:

| Key | Stock / live | Needed |
|---|---|---|
| `PlayerSave.Stats.MinLevel` | `0` (disabled) | `1` (every character) |
| `PlayerSave.Stats.SaveOnlyOnLogout` | `1` | `0` (write on every save) |

With both set, rows refresh on every `PlayerSaveInterval`, which is already
5000 ms on this realm. The row is written in the **same save** as
`characters.health` and `.power1-7`, so current and max values come from one
consistent snapshot. On logout the last values stay in the table, so offline
characters keep bars too, once they've logged in after the change.

Live state (read-only queries):

```text
mysql> SELECT COUNT(*) FROM characters.character_stats;
0
mysql> SHOW CREATE TABLE characters.character_stats;   -- table present, schema as above
```

This corrects the ROADMAP line "no persisted max health/max power column
anywhere". The column exists, but nothing writes to it on this realm yet.

Costs and caveats:

- Write cost is one extra `DELETE` + `INSERT` per online character per 5 s,
  appended to a save transaction that already rewrites inventory, spells,
  auras, skills and more. That's negligible at this realm's scale.
- Freshness is up to 5 s, the same as the current-value columns the drawer
  would already show.
- Power semantics match `characters.powerN`: 1 mana, 2 rage, 3 focus, 4
  energy, 5 happiness, 6 runes, 7 runic power. Rage and runic power are
  stored ×10 (max 1000 shows as 100 in game).
- Characters that haven't logged in since the change have no row, so the API
  must `LEFT JOIN` and the UI must fall back to numbers only.
- Applying it is a config change the image regenerates on start
  (`/app/backend/models/initializer/ConfigurationWriter.js` rewrites
  `worldserver.conf` from `TC_WORLD__*` env vars, turning `__` into `.`). It
  belongs in `docker-compose.yml`:
  `TC_WORLD__PlayerSave__Stats__MinLevel=1` and
  `TC_WORLD__PlayerSave__Stats__SaveOnlyOnLogout=0`, then a worldserver
  restart through the normal deploy. `.reload config` (`Console::Yes`, calls
  `World::LoadConfigSettings(true)`, which re-reads both keys) could apply it
  without a restart, but only a deliberate operator action should touch the
  live server. This spike did not.

### (a) Formula approximation

Base HP/mana by class and level ignores gear, talents, buffs and stamina, so
it would be visibly wrong for most characters. (d) makes it unnecessary.

### (b) Agent perception (`UNIT_FIELD_MAXHEALTH`, after UM-33)

It gives exact values but is range-limited to units near an agent. It's
still worth having for the agent's own decisions, but it isn't a source for
an operator panel.

### (c) C++ script/module

Rejected and unnecessary: the prebuilt image already carries the stock
feature in (d).

## Recommendation and effort

1. **No-go on RA/GM commands.** Don't build an RA poller for health/power.
2. **Go with `character_stats`.** Follow-up card: "Console Phase B2: HP/power
   bars via character_stats (PlayerSave.Stats.*)".
   - Compose env keys + deploy/restart + verify rows appear (`SELECT COUNT(*)
     FROM characters.character_stats` grows as characters save): ~0.5 h,
     needs an operator.
   - wowmap `fetch_character()`: `LEFT JOIN characters.character_stats s ON
     s.guid = c.guid`, return `health`, `power1-7`, `maxhealth`,
     `maxpower1-7` (or `null`), and map the class's primary power: ~1–2 h
     including a unit test with a fixture row. wowmap connects as MySQL
     `root` today, so no grant change is needed.
   - Inspect drawer bars (health + primary power, rage/runic ÷10, numbers-only
     fallback when max is `null`): ~2–3 h.

   Total: roughly **1 day**, no core changes, no new service.
3. If RA scripting is needed later for other reasons, reuse the flow in §2:
   one persistent session per consumer, read to `TC>`, and an existing
   level-3 realm `-1` account from a secret rather than the compose default
   password.
