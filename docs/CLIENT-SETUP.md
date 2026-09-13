# Client Setup

Connect a WoW 3.3.5a (build 12340) client to the private server.

## Client Version

Must be **3.3.5a.12340** (Wrath of the Lich King, final patch).
The login screen shows version in the bottom-left corner.

## realmlist.wtf

Edit `Data/enUS/realmlist.wtf` (or `Data/<locale>/realmlist.wtf`):

```
set realmlist 192.168.1.60
set patchlist 192.168.1.60
```

If the file doesn't exist, create it.

For a Portuguese client: edit `Data/ptBR/realmlist.wtf` or `Data/ptPT/realmlist.wtf`.

## Launch

Run `Wow.exe` directly — NOT the launcher. The launcher will try to patch
the client and break TrinityCore compatibility.

Recommended: create a shortcut to `Wow.exe` and use it to launch.

## Account Creation

Accounts are created server-side (not in-client):

### Via worldserver console

```bash
# On pandora
docker attach trinitycore-wowserver
# At the TC> prompt:
account create myuser mypassword
account set gmlevel myuser 3 -1
# Ctrl+P, Ctrl+Q to detach
```

### Via web UI

Open `http://192.168.1.60:3000` in a browser — use the account management panel.

GM level 3 = Administrator (all commands).

## First Login

1. Launch Wow.exe
2. Enter username and password
3. Select realm "Pandora"
4. Create character and enter world

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Unable to connect" | Check realmlist.wtf points to 192.168.1.60 |
| "Login server down" | Check docker compose is running: `docker compose ps` |
| "Disconnected from server" | Check worldserver is healthy: `docker logs trinitycore-wowserver` |
| "Wrong client version" | Must be 3.3.5a.12340 exactly |
| "The information you entered is not valid" | Account doesn't exist — create one server-side |

## Getting a 3.3.5a Client

The client is NOT included in this repository. You need a legitimate copy of
World of Warcraft: Wrath of the Lich King.

TrinityCore only supports the final 3.3.5a.12340 build — earlier 3.x builds
will not connect.