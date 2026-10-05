# world-mock

Scripted, stdlib-only fake WoW 3.3.5a auth + world server (#253). It lets CI run
the agent's **real** client code over localhost sockets: SRP6 login, realm list,
world auth with RC4 header crypt, character enum, login burst of update
objects, a quest turn-in action and the server's confirmation. No LAN, no
TrinityCore. Sibling of `tools/jev-mock`.

```bash
python3 tools/world-mock/server.py        # auth :13724, world :18085
python3 -m unittest discover -s tools/world-mock/tests -v   # what CI runs
```

Env: `HOST`, `AUTH_PORT`, `WORLD_PORT`, `MOCK_ACCOUNT`, `MOCK_PASSWORD` (defaults
are made-up test credentials, `MOCKUSER` / `mockpass`).

## What is real and what is faked

| Part | Status |
|---|---|
| SRP6 | Real server side (TrinityCore `SRP6.cpp`): verifier from the made-up password, `B = 3v + g^b`, `S = (A v^u)^b`, session-key interleave, M1 check, M2. A wrong password is rejected. Reuses `agent/auth.py` constants and `_sha1_interleave`. |
| World auth + crypt | Real: `CMSG_AUTH_SESSION` digest is verified, headers RC4-encrypted with `agent.crypt.WorldCrypt` (directions swapped). |
| `SMSG_CHAR_ENUM` | Captured fixture `fixtures/char_enum/three_characters.bin`. |
| Own-player CREATE block | Captured fixture `fixtures/update_object/login_self_create.bin`. |
| `SMSG_QUEST_QUERY_RESPONSE` | Captured fixture `fixtures/quests/quest_query_response_8325.bin`. |
| Questgiver CREATE block, quest-log VALUES, `OFFER_REWARD`, `QUEST_COMPLETE` | **Built**, not captured (no live capture exists): `agent/tests/builders.py`, layouts cited from TrinityCore 3.3.5 `QuestPackets.cpp` / `Player.cpp`. The agent's parsers consuming them proves self-consistency with the parser, not with a live server. |
| Not modelled | client-build CRC (`crc_hash`) and security flags, bans, 2FA, addon info, tutorial flags (zeros), game rules (range, quest state), anything past this one scenario. Unknown opcodes are recorded in `mock.received` and ignored. |

To script another flow, add a branch to `WorldMock._on_packet` and use / extend the
builders in `agent/tests/builders.py`; prefer a captured fixture when one exists.
