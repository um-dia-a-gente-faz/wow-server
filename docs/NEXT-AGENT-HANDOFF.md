Continue implementing the WoW 3.3.5a AI agent in this repo. Your task: parse
update-object packets to give the agent perception.

SETUP
  git clone git@github.com:Cividati/wow-server.git
  cd wow-server
  git checkout feat/agent-client-protocol
  # Python 3.10+, pure stdlib — zero pip installs needed

WHAT EXISTS
  Branch feat/agent-client-protocol. Protocol client works end-to-end:
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
    export WOW_ACCOUNT=AGENT01 WOW_PASSWORD=hunter123 WOW_CHARACTER=Luaprata
    python3 -m agent --dry-run   # logs in for 30s, says hello, exits

  Live server: 192.168.1.64:3724 (auth), 8085 (world), 3000 (web UI)
  Characters: guid 1 Rubens (human), guid 2 Luaprata (agent)

WHAT NEEDS TO BE DONE — Layer 1: Perception

  Parse SMSG_UPDATE_OBJECT / SMSG_COMPRESSED_UPDATE_OBJECT so the agent
  can see its own position, nearby creatures, and nearby players.
  Currently _parse_update_object() in session.py only tracks GUIDs.

  Compressed packet (already decompressed in _handle_compressed):
    payload = uint32 uncompressed_size + zlib-compressed data
    decompressed = zlib.decompress(payload[4:])

  Decompressed layout (SMSG_UPDATE_OBJECT, opcode 0x1F7 after inflation):

    uint32 block_count    — number of object update blocks

    Each block:
      uint8 update_type
        0 = VALUES         — update existing object (has mask + fields)
        1 = MOVEMENT       — movement data only
        2 = CREATE_OBJECT  — new object with full mask + movement
        3 = CREATE_OBJECT2 — alternate create

      packed GUID (use agent.packets.unpack_packed_guid):
        uint8 mask, then up to 8 data bytes (one per set bit)

      IF update_type in (VALUES, CREATE_OBJECT, CREATE_OBJECT2):
        uint32 update_flags (bitmask — see GUID documents below)
        IF flags & 0x00000010 (UPDATEFLAG_LIVING): movement block
          uint32 movement_flags
          // ... movement fields (position, orientation, speeds, etc.)
          float x, y, z
          float orientation
          IF flags & UPDATEFLAG_HAS_POSITION: float pos_x, pos_y, pos_z
        uint32 mask_length (byte) — if mask_length < 128, that's the byte count
        uint32[mask_length] mask_words (little-endian)
        uint8[mask_length * 4] mask (reinterpret as little-endian bitmask)
        uint32 values[masked_bits] — one uint32 per set bit in the mask

  Object field indices to extract (from the mask, low bits are low field IDs):
    OBJECT_FIELD_GUID          0x0000
    OBJECT_FIELD_TYPE          0x0001
    OBJECT_FIELD_ENTRY         0x0002
    OBJECT_FIELD_SCALE_X       0x0004
    UNIT_FIELD_HEALTH          0x0021
    UNIT_FIELD_MAXHEALTH       0x0024
    UNIT_FIELD_LEVEL           0x002B
    UNIT_FIELD_FACTIONTEMPLATE 0x002C
    UNIT_NPC_FLAGS             0x0039
    PLAYER_FLAGS               0x004E

  The mask is a variable-length bitfield. Read mask_length bytes,
  re-assemble as little-endian uint32 words, then iterate bits.
  For each set bit N, read one uint32 as the value for field index N.

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
    user trinity, pass trinity, databases characters/world/auth.
  SSH to 192.168.1.64 as root (key at ~/.ssh/id_ed25519).

DOCKER (already working)
  docker build -t wow-agent .
  docker run --rm -e WOW_ACCOUNT=AGENT01 -e WOW_PASSWORD=hunter123 \
      -e WOW_CHARACTER=Luaprata wow-agent:latest --dry-run
  # docker-compose.agents.yml has 5-agent template (currently only
  # agent-luaprata is uncommented — Farstrider/Shadowblade/Sunspeaker/
  # Spellweaver exist as chars on accounts AGENT02..AGENT05 with the
  # same password 'hunter123')

VERIFY YOUR WORK BY
  1. Running python3 -m agent --dry-run — should log in and print
     at least "perception: N objects tracked" with N > 0
  2. Checking that session.player_position updates when the player
     moves (send a CMSG_MOVE_START_FORWARD or use .go xyz from the
     web UI to teleport the character, then observe position change)
  3. Confirming nearby NPCs appear in the object list with entry IDs