# GM Commands

Quick reference for common GM commands. Full list:
https://trinitycore.atlassian.net/wiki/spaces/tc/pages/2130065/gm+commands

## Access the Console

```bash
docker attach trinitycore-wowserver
# Press Enter to get the TC> prompt
# Ctrl+P, Ctrl+Q to detach
```

Or type commands in-game with `.` prefix (requires GM level).

## Account Management

```
account create <user> <pass>          Create account
account delete <user>                 Delete account
account set gmlevel <user> 3 -1       Set GM level (3=admin, -1=all realms)
account set addon <user> 2            Allow addons (0=no, 1=yes, 2=bypass)
account onlinelist                    List online accounts
```

## Character Commands

```
.character level 80                   Set level
.learn all_crafts                     Learn all professions
.learn all_gm                         Learn all GM spells
.learn all_lang                       Learn all languages
.learn all_myclass                    Learn all class spells
.modify money 10000000                Add gold (in copper; 100g = 1000000)
.additem 47241                        Add item (Emblem of Triumph)
.learn <spell_id>                     Learn a specific spell
.reset talents                        Reset talent points
.maxskill                             Max all skills
```

## Teleportation

```
.go xyz <x> <y> <z> <map>            Teleport to coordinates
.gps                                  Show current coordinates
.tele <location>                      Teleport to named location
.namego <player>                      Teleport to player
.goname <player>                      Teleport to player
.recall <port>                        Teleport to saved location
```

## Useful Items

```
.additem 23162    Bag of Holding (36 slot)
.additem 47241    Emblem of Triumph
.additem 40752    Emblem of Heroism
.additem 13335    Deathcharger's Reins (mount)
.additem 54811    Celestial Steed (mount)
.additem 41508    Mechano-Hog (mount)
.additem 34092    Merciless Gladiator's Frost Wyrm
```

## Misc

```
.server info                          Server info
.server motd <message>                Set MOTD
.announce <message>                   Server-wide announcement
.saveall                              Save all characters
.gm on/off                            Toggle GM mode
.modify speed 2                       Set movement speed multiplier
.respawn                               Respawn nearby creatures
.die                                   Kill yourself
.revive                                 Revive yourself
```

## GM Mode

In-game: `.gm on` makes you invisible and untargetable.
`.gm off` returns to normal.

GM level 3 = full access to all commands.
GM level 1 = basic moderation (kick, mute).
GM level 0 = normal player.