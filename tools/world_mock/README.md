# world_mock

Scripted, stdlib-only fake WoW 3.3.5a auth + world server (#253). It lets CI run
the agent's **real** client code over localhost sockets: SRP6 login, realm list,
world auth with RC4 header crypt, character enum, login burst of update
objects, a quest turn-in action and the server's confirmation. No LAN, no
TrinityCore. Sibling of `tools/jev-mock`.

```bash
python3 -m tools.world_mock.server        # auth :13724, world :18085
python3 -m unittest discover -s tools/world_mock/tests -v   # what CI runs
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
MOCK_FAULTS='drop_after=8,reset;split_write=3' python3 -m tools.world_mock.server
```

World packets are numbered from 1 after the unencrypted `SMSG_AUTH_CHALLENGE`:
1 `AUTH_RESPONSE`, 2 `TIME_SYNC_REQ`, 3 `TUTORIAL_FLAGS`, 4 `CHAR_ENUM`,
5 `LOGIN_VERIFY_WORLD`, 6-8 the login burst, then the scripted replies.

| Field | Fault | What the test asserts (`tests/test_faults.py`) |
|---|---|---|
| `drop_after=N` | hang up (FIN) after N world packets | supervisor reconnects exactly once, backoff resets after a good login, no thread left |
| `reset` | with `drop_after`: RST instead of FIN | same; a RST also discards unread data, so it may land during login |
| `truncate` | with `drop_after`: only half of packet N is sent | a partial packet ends the session, one reconnect |
| `stall=S`, `stall_at=N` | S seconds of silence, socket open, before packet N | short: think loop keeps its period, session carries on. Longer than the dead-socket deadline: one reconnect (#404), keepalive and ping sent meanwhile |
| `split_write=B` | every write in B-byte chunks, 0.5 ms apart | `Transport` reassembles headers and payloads, nothing dropped |
| `corrupt_opcode=OP` | that opcode's payload becomes `0xFF` bytes | `dropped_packets` counts it, session and framing survive |
| `bad_crypt_from=N` | headers unencrypted from packet N (RC4 desync) | mid-packet timeout ends the session, one reconnect, no busy loop |
| `slow_auth=S` | auth server answers the challenge after S seconds | login still succeeds |
| `auth_reject=CODE` | logon challenge fails with that `AuthResult`: 3 bytes, socket left open as TrinityCore does | login raises `AuthRejected` naming the code in well under a second, world server never contacted, backoff doubles |
| `login_silence=S` | S seconds between `CMSG_PLAYER_LOGIN` and `SMSG_LOGIN_VERIFY_WORLD`, mock still reading | login waits without pinging (#426); `mock.ping_times` has every ping arrival |
| `overspeed_s=T` | TrinityCore `HandlePing` rule: pings closer than T s count, the 3rd one hangs up (T = 27 in TrinityCore, tests scale it down) | no two pings closer than T, no kick |

Every test also asserts that the password is in no log record and that the thread
count is back to where it started.

Fields compose (`FaultPlan(split_write=3, drop_after=8, truncate=True)`). Known limits:

- An unknown account and a wrong password are rejected like `auth_reject=4`: the failure bytes, then the socket stays open until the client hangs up.
- The mock answers `CMSG_PING` with `SMSG_PONG` (echoing the id) and sends nothing periodically, so a
  stall longer than `session.DEAD_SOCKET_TIMEOUT_S` ends the session (#404). Tests shorten the
  keepalive, ping and deadline constants with `mock.patch.object`.
- Plans are handed out per login attempt in arrival order and looked up by account, so
  with several agents on one account the assignment is not deterministic.

### Soak harness (#299)

Run the mock as a process and let the fault list repeat for as long as the soak runs:

```bash
MOCK_FAULTS=';;;drop_after=8,reset;;stall=20,stall_at=6;bad_crypt_from=6' \
MOCK_FAULTS_LOOP=1 python3 -m tools.world_mock.server
```

`MOCK_FAULTS_LOOP=1` cycles the list (in code: `faults=itertools.cycle(plans)`), so the
example gives three healthy logins, a reset, a healthy one, a 20 s stall and a desync,
then starts over. Packet numbers count what the mock sends on one connection: 8 packets
per login and only a handful of scripted replies after that (it does not answer
`CMSG_PING` or `CMSG_KEEP_ALIVE`), so a fault set past 8 never fires. The harness reads recovery from the agent side (reconnect count, time
back to "online", thread count, `dropped_packets`).

## Lifecycle scenarios (#292)

`tests/test_lifecycle_scenarios.py` builds a whole agent with `agent.root.build_agent()` from a
real `Config` and runs `Agent.run()` over localhost sockets against a world_mock with a
`FaultPlan` (or `DeathMock`). Each scenario checks the lifecycle lines in the audit directory
(ADR 0015), the backoffs the supervisor asked for (the sleep is injected, never slept) and the
connections the mock saw.

| Test | World-mock | Lifecycle lines expected | Backoffs |
|---|---|---|---|
| `test_reconnect_scenarios`: healthy session ends with its duration | `WorldMock()` | `connecting>online connected`, `online>stopping run_ended`, `stopping>stopped run_ended` | none |
| `test_reconnect_scenarios`: dropped session reconnects once | `FaultPlan(drop_after=8)` | the six lines of a dropped session (`online>backoff disconnected`, `backoff>connecting backoff_elapsed`, `connected`, `run_ended` twice) | `[1.0]` |
| `test_reconnect_scenarios`: truncated packet reconnects once | `FaultPlan(drop_after=6, truncate=True)` | the same six lines | `[1.0]` |
| `test_reconnect_scenarios`: transient auth refusals back off and recover | `FaultPlan(auth_reject=6)` twice | `connecting>backoff auth_rejected`, `backoff>connecting backoff_elapsed`, both again, then the three lines of a clean run | `[1.0, 2.0]` |
| `test_permanent_auth_refusal_fails_after_one_attempt` | `FaultPlan(auth_reject=3)` | `connecting>failed auth_rejected_permanent`; `AuthRejected` leaves `run()` | none |
| `test_stop_during_a_session_logs_out_once_and_does_not_reconnect` | `WorldMock()`, stop set 0.2 s after `online` | `connected`, `online>stopping stop`, `stopping>stopped stop` | none |
| `test_a_crash_in_the_session_still_logs_out_and_ends_stopped_crashed` | `WorldMock()`, `root.run_loop` raises | `connected`, `online>stopping crashed`, `stopping>stopped crashed` | none |
| `test_death_is_game_state_and_not_a_lifecycle_state` | `DeathMock(faults=[FaultPlan(drop_after=12)])` | the six lines of a dropped session; no line mentions death | `[1.0]` |

Every test also checks that the mock's password is in no log record and that no thread is left.

## Per-client stream (#421)

`WorldMock(stream_hz=N)` / env `MOCK_STREAM_HZ=N`: after login each client gets N ticks a
second of 3 packets: a CREATE_OBJECT2 unit (LIVING|STATIONARY_POSITION movement block with MovementInfo and 9 speeds, sent as SMSG_COMPRESSED_UPDATE_OBJECT as TrinityCore does above 100 bytes; guid cycles through `stream_pool` (default 50; 0 = every tick a new unit, so `objects` grows, #486) and nothing goes out of range), an SMSG_MONSTER_MOVE
stop for it, and an SMSG_MESSAGECHAT say from a cycling player guid. Default 0 = nothing. `WorldMock(stream_pool=N)` / env `MOCK_STREAM_POOL=N` (#486) sets the guid cycle; 0 = unbounded.
`tools/loadtest --stream-hz` drives it. Layouts: docs/PROTOCOL-NOTES.md (TrinityCore 3.3.5).
Stream packets count towards a fault plan's `drop_after`/`stall_at`, so those can fire with `stream_hz > 0`.
