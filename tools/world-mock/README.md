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
are made-up test credentials, `MOCKUSER` / `mockpass`), `MOCK_FAULTS`,
`MOCK_FAULTS_LOOP` (see *Fault injection*).

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

## Fault injection (#339)

A `FaultPlan` says what goes wrong during **one login attempt**: one auth connection
and the world connection that follows it. The default plan injects nothing, so a mock
built without `faults` behaves as before. Plans are consumed in order, one per attempt;
attempts past the end of the list are healthy, which is what lets a test assert
"fault, then one clean reconnect".

```python
mock = server.WorldMock(faults=[FaultPlan(drop_after=8, reset=True), FaultPlan(split_write=3)])
mock.auth_connections, mock.world_connections      # reconnect count
```

```bash
# same thing from the environment: ';' between attempts, ',' between fields, bare flag = 1
MOCK_FAULTS='drop_after=8,reset;split_write=3' python3 tools/world-mock/server.py
```

World packets are numbered from 1 after the unencrypted `SMSG_AUTH_CHALLENGE`:
1 `AUTH_RESPONSE`, 2 `TIME_SYNC_REQ`, 3 `TUTORIAL_FLAGS`, 4 `CHAR_ENUM`,
5 `LOGIN_VERIFY_WORLD`, 6-8 the login burst, then the scripted replies.

| Field | Fault | What the test asserts (`tests/test_faults.py`) |
|---|---|---|
| `drop_after=N` | hang up (FIN) after N world packets | supervisor reconnects exactly once, backoff resets after a good login, no thread left |
| `reset` | with `drop_after`: RST instead of FIN | same; a RST also discards unread data, so it may land during login |
| `truncate` | with `drop_after`: only half of packet N is sent | a partial packet ends the session, one reconnect |
| `stall=S`, `stall_at=N` | S seconds of silence, socket open, before packet N | think loop keeps its period, recv thread alive, session carries on afterwards |
| `split_write=B` | every write in B-byte chunks, 0.5 ms apart | `Transport` reassembles headers and payloads, nothing dropped |
| `corrupt_opcode=OP` | that opcode's payload becomes `0xFF` bytes | `dropped_packets` counts it, session and framing survive |
| `bad_crypt_from=N` | headers unencrypted from packet N (RC4 desync) | mid-packet timeout ends the session, one reconnect, no busy loop |
| `slow_auth=S` | auth server answers the challenge after S seconds | login still succeeds |
| `auth_reject=CODE` | logon challenge fails with that `AuthResult` | login raises, world server never contacted, backoff doubles |

Every test also asserts that the password is in no log record and that the thread
count is back to where it started.

Fields compose (`FaultPlan(split_write=3, drop_after=8, truncate=True)`). Known limits:

- `auth_reject` hangs up after the 3-byte failure; TrinityCore leaves the socket open,
  which the agent does not handle yet (#405).
- A stall that never ends is not detected by the agent at all (#404), so the test only
  covers a stall the session should survive. The 15 s keepalive is not asserted.
- After a desync the agent's `logout()` still waits on the dead stream (#406).
- Plans are handed out per login attempt in arrival order and looked up by account, so
  with several agents on one account the assignment is not deterministic.

### Soak harness (#299)

Run the mock as a process and let the fault list repeat for as long as the soak runs:

```bash
MOCK_FAULTS=';;;drop_after=8,reset;;stall=20,stall_at=6;bad_crypt_from=6' \
MOCK_FAULTS_LOOP=1 python3 tools/world-mock/server.py
```

`MOCK_FAULTS_LOOP=1` cycles the list (in code: `faults=itertools.cycle(plans)`), so the
example gives three healthy logins, a reset, a healthy one, a 20 s stall and a desync,
then starts over. Packet numbers count what the mock sends on one connection: 8 packets
per login and only a handful of scripted replies after that (it does not answer
`CMSG_PING` or `CMSG_KEEP_ALIVE`), so a fault set past 8 never fires. The harness reads recovery from the agent side (reconnect count, time
back to "online", thread count, `dropped_packets`).
