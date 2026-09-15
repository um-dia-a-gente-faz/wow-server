Continue implementing the WoW 3.3.5a AI agent in this repo. Your task: parse
update-object packets to give the agent perception.

SETUP
  git clone git@github.com:Cividati/wow-server.git
  cd wow-server
  # Work from main. The protocol client branch is already merged.
  # Python 3.10+, pure stdlib — zero pip installs needed

WHAT EXISTS
  On main, the protocol client works end-to-end:
    - SRP6 auth (LE wire format, uppercase password, skip-interleave)
    - World handshake with HMAC-SHA1-RC4 crypto (TrinityCore WorldPacketCrypt)
    - Character login (Luaprata guid 2, blood-elf paladin lvl 1)
    - Background keepalive (time-sync + pings)
    - Graceful logout — online=1 during session, back to 0 after

  Key files:
    agent/session.py   — WoWSession (login, bg recv loop, _handle_compressed)
    agent/crypt.py     — RC4 + WorldPacketCrypt
    agent/auth.py      — SRP6 auth client
    agent/packets.py   — binary helpers, packed GUID, sha1
    agent/perception.py— WorldState STUB (tracks GUIDs only, no field data)
    agent/actions.py   — chat (say/yell/whisper), party invite, target (WORKING)
    agent/config.py    — env-based config
    agent/__main__.py  — CLI: --list-chars, --dry-run, forever loop
    Dockerfile         — python:3.12-slim, no pip deps
    docker-compose.agents.yml — one container per agent

  Test (proves auth+login works):
    export WOW_ACCOUNT=AGENT01 WOW_PASSWORD='<see .env>' WOW_CHARACTER=Luaprata
    python3 -m agent --dry-run   # logs in for 30s, says hello, exits

  Live server: 192.168.1.64:3724 (auth), 8085 (world), 3000 (web UI)
  Characters: guid 1 Rubens (human), guid 2 Luaprata (agent)

WHAT NEEDS TO BE DONE — Layer 1: Perception

  Parse SMSG_UPDATE_OBJECT / SMSG_COMPRESSED_UPDATE_OBJECT so the agent
  can see its own position, nearby creatures, and nearby players.
  Currently _parse_update_object() in session.py only tracks GUIDs.

  Packet layout, update flags and field indices: see docs/PROTOCOL-NOTES.md.
  Every entry there was checked against TrinityCore 3.3.5 source. The work
  itself is split into Linear UM-32 (block framing + movement block) and
  UM-33 (VALUES_UPDATE mask + field mapping). Don't parse from memory: the
  3.3.5a layout differs from older write-ups in several places. For example,
  VALUES blocks carry no update flags, and update flags are uint16.

  Target: after parsing, _parse_update_object should populate
  perception.WorldState.objects with entries that carry:
    guid, entry_id (creature/object template ID), name (look up from
      creature_template or client databases — or store entry_id for now),
    position (x, y, z, map), level, health, faction

  The agent's own GUID is identified from the first CREATE_OBJECT block
  after login (set it on perception.WorldState.my_guid).  After the
  player is created, subsequent MOVEMENT blocks with the same GUID update
  position in-place.

  Once perception works, connect it to the agent loop in agent/__main__.py.
  The loop prints every object the agent can see each cycle.

REFERENCE MATERIAL
  TrinityCore 3.3.5 source is on GitHub (branch 3.3.5).  Key files:
    src/server/game/Server/Protocol/Opcodes.cpp    — opcode mapping
    src/server/game/Entities/Object/Object.cpp     — BuildCreateUpdateBlockForPlayer,
                                                     BuildMovementUpdate, BuildValuesUpdate
    src/server/game/Entities/Object/Updates/UpdateData.cpp — packet framing, compression
    src/server/game/Entities/Object/Updates/UpdateFields.h — field indices
    src/server/game/Entities/Object/ObjectGuid.cpp — packed-GUID operator>>
    src/server/game/Server/WorldSocket.cpp         — crypto setup
    src/server/game/Server/Packets/AuthenticationPackets.cpp
    src/common/Cryptography/Authentication/WorldPacketCrypt.cpp
    src/common/Cryptography/Authentication/SRP6.cpp

  The docker image on the server is danielsilvestre37/trinitycore-docker:3.3.5
  (rev 2ac2d9055061, 2026-05-29, Linux x86_64, RelWithDebInfo, static).

  The existing MCP runtime is in agent-runtime/ (Node.js) — ignore it.
  It was the pre-protocol fake layer using MySQL + GM commands.  The new
  Python agent/ package IS the real client.

  If you need to look at DB data, the MySQL at 192.168.1.64:3306 has:
    user trinity, pass <see .env> (TRINITY_DB_PASSWORD), databases characters/world/auth.
  SSH to 192.168.1.64 as root (key at ~/.ssh/id_ed25519).

DOCKER (already working)
  docker build -t wow-agent .
  docker run --rm -e WOW_ACCOUNT=AGENT01 -e WOW_PASSWORD='<see .env>' \
      -e WOW_CHARACTER=Luaprata wow-agent:latest --dry-run
  # docker-compose.agents.yml has 5-agent template (currently only
  # agent-luaprata is uncommented — Farstrider/Shadowblade/Sunspeaker/
  # Spellweaver exist as chars on accounts AGENT02..AGENT05 with the
  # same password: AGENT_PASSWORD in .env)

VERIFY YOUR WORK BY
  1. Running python3 -m agent --dry-run — should log in and print
     at least "perception: N objects tracked" with N > 0
  2. Checking that session.player_position updates when the player
     moves (send a CMSG_MOVE_START_FORWARD or use .go xyz from the
     web UI to teleport the character, then observe position change)
  3. Confirming nearby NPCs appear in the object list with entry IDs