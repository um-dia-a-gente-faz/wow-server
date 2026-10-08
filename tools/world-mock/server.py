#!/usr/bin/env python3
"""world-mock: a scripted, stdlib-only fake WoW 3.3.5a auth + world server (#253).

It lets CI drive the agent's real client code (agent.auth.auth_logon,
WoWSession.connect/enum_characters/login_character, the recv thread, the
router/handlers and the actions) over real sockets: login -> update objects ->
action -> server confirmation, with no LAN and no TrinityCore.

Scripted scenario ("quest turn-in"), served to any character:

  auth    real SRP6 on the server side (see _Srp6Server), realm list with this
          mock's world port.
  world   SMSG_AUTH_CHALLENGE -> CMSG_AUTH_SESSION (digest verified) -> RC4
          header crypt (agent.crypt.WorldCrypt with the directions swapped) ->
          SMSG_AUTH_RESPONSE -> CMSG_CHAR_ENUM answered with the captured
          fixtures/char_enum/three_characters.bin -> CMSG_PLAYER_LOGIN answered
          with LOGIN_VERIFY_WORLD, the captured own-player CREATE block
          (fixtures/update_object/login_self_create.bin), a scripted questgiver
          (Magistrix Erona's guid from docs/AGENT-RUN-1-10.md) and a VALUES
          block putting quest 8325 in the log, complete.
          CMSG_QUEST_QUERY -> captured fixtures/quests/quest_query_response_8325.bin.
          CMSG_QUESTGIVER_COMPLETE_QUEST -> SMSG_QUESTGIVER_OFFER_REWARD.
          CMSG_QUESTGIVER_CHOOSE_REWARD -> SMSG_QUESTGIVER_QUEST_COMPLETE and a
          VALUES block clearing the quest-log slot.
          CMSG_LOGOUT_REQUEST -> SMSG_LOGOUT_COMPLETE. Every other client
          opcode is recorded in `received` and ignored.

What it fakes (and why it is enough): the account store is a dict of made-up
credentials; the client-build CRC (crc_hash), 2FA/security flags, bans, the
realm DB, addon info, tutorial flags (zeros) and the world itself are not
modelled. The offer-reward / quest-complete / own-player-values / NPC-create
payloads are built with agent/tests/builders.py (layouts cited there) because
no live capture of those exists yet; `received` lets a test assert what the
agent sent. The mock does not validate game rules (range, quest state).

Fault injection (#339): WorldMock(faults=[FaultPlan(...), ...]) or MOCK_FAULTS makes
a login attempt misbehave (drop, reset, truncate, stall, split writes, corrupt
payload, bad header crypt, slow or rejected auth). See FaultPlan and the README.

    python3 tools/world-mock/server.py     # auth :13724, world :18085
Environment: HOST, AUTH_PORT, WORLD_PORT, MOCK_ACCOUNT, MOCK_PASSWORD, MOCK_FAULTS,
MOCK_FAULTS_LOOP.
"""

import dataclasses
import itertools
import logging
import os
import pathlib
import socket
import struct
import sys
import threading
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent import auth as au  # noqa: E402
from agent import crypt as cr  # noqa: E402
from agent import opcodes as op  # noqa: E402
from agent import packets as pk  # noqa: E402
from agent import quests as qu  # noqa: E402
from agent import update_fields as uf  # noqa: E402
from agent import update_object as uo  # noqa: E402
from agent.tests import builders as bd  # noqa: E402

log = logging.getLogger("world-mock")

FIXTURES = REPO / "agent" / "tests" / "fixtures"

# Made-up credentials; never real ones.
DEFAULT_ACCOUNT = "MOCKUSER"
DEFAULT_PASSWORD = "mockpass"

# TrinityCore 3.3.5 common/Cryptography/Authentication/SRP6.cpp: N (hex, big
# endian; the wire is little endian), g = 7, k = 3.
SRP_N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
SRP_G = 7

# Scripted world.
SELF_GUID = 2                      # Luaprata in char_enum/three_characters.bin
MAP_ID = 530
SELF_POS = (10344.900390625, -6354.1201171875, 32.60350036621094, 0.0)  # login_self_create.bin
QUESTGIVER_GUID = 17379391218345059262   # Magistrix Erona, docs/AGENT-RUN-1-10.md
QUESTGIVER_ENTRY = (QUESTGIVER_GUID >> 24) & 0xFFFFFF
QUEST_ID = 8325
QUEST_TITLE = "Reclaiming Sunstrider Isle"
REWARD_CHOICES = ((20997, 1, 0), (20998, 1, 0))   # item ids from fixtures/quests/README.md
REWARD_MONEY = 30
REALM_ID = 1
UNIT_NPC_FLAG_QUESTGIVER = 0x2


def _le(n: int) -> bytes:
    return n.to_bytes(32, 'little')


class _Srp6Server:
    """Server half of TrinityCore's SRP6 (SRP6.cpp), the mirror of
    agent.auth.compute_srp6: v = g^x, B = 3v + g^b, S = (A v^u)^b, K =
    SHA1Interleave(S), M2 = H(A, M1, K)."""

    def __init__(self, username: str, password: str):
        self.user = username.upper().encode('ascii')
        self.salt = os.urandom(32)
        x = int.from_bytes(pk.sha1(self.salt, pk.sha1(self.user + b':' + password.upper().encode('ascii'))), 'little')
        self.v = pow(SRP_G, x, SRP_N)
        self._b = int.from_bytes(os.urandom(32), 'little') % SRP_N
        self.B = _le((3 * self.v + pow(SRP_G, self._b, SRP_N)) % SRP_N)

    def verify(self, A: bytes, client_m1: bytes):
        """Returns (session_key, M2) or None when the proof is wrong."""
        A_int = int.from_bytes(A, 'little')
        if A_int % SRP_N == 0:
            return None
        u = int.from_bytes(pk.sha1(A, self.B), 'little')
        S = _le(pow(A_int * pow(self.v, u, SRP_N), self._b, SRP_N))
        K = au._sha1_interleave(S)
        n_hash = bytes(a ^ b for a, b in zip(pk.sha1(_le(SRP_N)), pk.sha1(bytes([SRP_G]))))
        if pk.sha1(n_hash, pk.sha1(self.user), self.salt, A, self.B, K) != client_m1:
            return None
        return K, pk.sha1(A, client_m1, K)


@dataclasses.dataclass(frozen=True)
class FaultPlan:
    """What goes wrong during one login attempt: one auth connection and the world
    connection that follows it. The default plan injects nothing. Fields compose.

    World packets are numbered from 1 after the unencrypted SMSG_AUTH_CHALLENGE:
    1 AUTH_RESPONSE, 2 TIME_SYNC_REQ, 3 TUTORIAL_FLAGS, 4 CHAR_ENUM,
    5 LOGIN_VERIFY_WORLD, 6-8 the login burst (self, questgiver, quest log), then
    the scripted replies."""
    drop_after: int = 0      # hang up once this many world packets were sent (0 = never)
    reset: bool = False      # ... with a TCP RST instead of a FIN
    truncate: bool = False   # ... and only the first half of that last packet is sent
    stall: float = 0.0       # seconds of silence (nothing sent or read, socket open) ...
    stall_at: int = 1        # ... before this world packet
    split_write: int = 0     # every write, auth and world, goes out in chunks of this many bytes
    corrupt_opcode: int = 0  # this opcode keeps its header, the payload becomes 0xFF bytes
    bad_crypt_from: int = 0  # from this world packet on, headers are sent unencrypted
    slow_auth: float = 0.0   # seconds before the auth server answers the logon challenge
    auth_reject: int = 0     # logon challenge fails with this AuthResult (AuthCodes.h), e.g. 3 banned

    @classmethod
    def parse(cls, spec: str) -> list:
        """MOCK_FAULTS: one plan per login attempt, ';' between plans, ',' between
        fields, a bare flag means 1. "drop_after=8,reset;;split_write=3" resets the
        first attempt, leaves the second alone and splits the third."""
        types = {f.name: f.type for f in dataclasses.fields(cls)}
        plans = []
        for part in spec.split(';'):
            kw = {}
            for item in filter(None, (i.strip() for i in part.split(','))):
                key, _, value = item.partition('=')
                kw[key] = float(value) if types[key] is float else types[key](int(value or '1', 0))
            plans.append(cls(**kw))
        return plans


NO_FAULTS = FaultPlan()
# Pause between the chunks of a split write, so the client really sees partial reads
# instead of the kernel handing it the coalesced packet.
SPLIT_WRITE_PAUSE_S = 0.0005


def _write(sock, data: bytes, plan: FaultPlan = NO_FAULTS):
    if not plan.split_write:
        return sock.sendall(data)
    for i in range(0, len(data), plan.split_write):
        sock.sendall(data[i:i + plan.split_write])
        time.sleep(SPLIT_WRITE_PAUSE_S)


def _recvn(sock, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("client closed")
        buf += c
    return buf


class WorldMock:
    def __init__(self, host="127.0.0.1", auth_port=0, world_port=0,
                 account=DEFAULT_ACCOUNT, password=DEFAULT_PASSWORD, faults=()):
        """`faults`: an iterable of FaultPlan, one per login attempt in order; attempts
        past its end get no faults (pass itertools.cycle(...) to repeat)."""
        self.host = host
        self._faults = iter(faults)
        self._plans = {}           # account -> FaultPlan of its current login attempt
        self.auth_connections = 0
        self.world_connections = 0
        self.accounts = {account.upper(): password}
        self.session_keys = {}     # account -> K of the last successful auth
        self.received = []         # (opcode, payload) of every post-login client packet
        self._lock = threading.Lock()
        self._auth = self._listen(auth_port, self._serve_auth)
        self._world = self._listen(world_port, self._serve_world)
        self.auth_port = self._auth.getsockname()[1]
        self.world_port = self._world.getsockname()[1]

    def _listen(self, port, handler):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, port))
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv, handler), daemon=True).start()
        return srv

    def _accept(self, srv, handler):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._guarded, args=(handler, conn), daemon=True).start()

    @staticmethod
    def _guarded(handler, conn):
        conn.settimeout(30)
        try:
            handler(conn)
        except (ConnectionError, socket.timeout):
            pass
        except Exception:
            log.exception("world-mock handler crashed")
        finally:
            conn.close()

    def close(self):
        for s in (self._auth, self._world):
            s.shutdown(socket.SHUT_RDWR)   # wakes the accept() thread; close() alone leaves it blocked
            s.close()

    def packets_received(self, opcode: int) -> list:
        with self._lock:
            return [p for o, p in self.received if o == opcode]

    # ── auth server (port 3724 protocol) ─────────────────────────────────

    def _serve_auth(self, sock):
        # AUTH_LOGON_CHALLENGE: cmd, error, uint16 size, then 'WoW\0', version(3), build(2),
        # platform(4), os(4), locale(4), timezone(4), ip(4), uint8 name length, name.
        with self._lock:
            self.auth_connections += 1
            plan = next(self._faults, NO_FAULTS)
        head = _recvn(sock, 4)
        body = _recvn(sock, pk.u16(head, 2))
        username = body[30:30 + body[29]].decode('ascii').upper()
        time.sleep(plan.slow_auth)
        if plan.auth_reject:
            # AuthSession::LogonChallengeCallback failure: cmd, 0x00, AuthResult. TrinityCore
            # leaves the socket open afterwards; the mock hangs up (see README, #405).
            _write(sock, bytes([au.AUTH_CMD_LOGON_CHALLENGE, 0, plan.auth_reject]), plan)
            return
        self._plans[username] = plan
        password = self.accounts.get(username)
        if password is None:
            return                                  # unknown account: just hang up
        srp = _Srp6Server(username, password)
        # AuthSession::HandleLogonChallenge success layout (AuthSession.cpp).
        _write(sock, bytes([au.AUTH_CMD_LOGON_CHALLENGE, 0, 0]) + srp.B + bytes([1, SRP_G, 32])
               + _le(SRP_N) + srp.salt + au.VERSION_CHALLENGE + bytes([0]), plan)
        # AUTH_LOGON_PROOF_C: cmd, A(32), M1(20), crc_hash(20), nKeys, securityFlags.
        proof = _recvn(sock, 75)
        res = srp.verify(proof[1:33], proof[33:53])
        if res is None:
            _write(sock, bytes([au.AUTH_CMD_LOGON_PROOF, 4]) + struct.pack('<H', 0), plan)
            return
        K, m2 = res
        self.session_keys[username] = K
        # sAuthLogonProof_S: cmd, error, M2, AccountFlags(4), SurveyId(4), LoginFlags(2).
        _write(sock, bytes([au.AUTH_CMD_LOGON_PROOF, 0]) + m2 + struct.pack('<IIH', 0, 0, 0), plan)
        _recvn(sock, 5)                              # REALM_LIST request: cmd + uint32
        # RealmListCallback (AuthSession.cpp): per realm type, locked, flags, name, address,
        # population, characters, timezone, id; then 0x10 0x00; header cmd + uint16 size,
        # body starts uint32 0 + uint16 realm count.
        realm = (bytes([1, 0, 0]) + b"WorldMock\0" + f"{self.host}:{self.world_port}".encode() + b"\0"
                 + struct.pack('<f', 0.0) + bytes([3, 1, REALM_ID]))
        body = struct.pack('<IH', 0, 1) + realm + bytes([0x10, 0x00])
        _write(sock, bytes([au.AUTH_CMD_REALM_LIST]) + struct.pack('<H', len(body)) + body, plan)

    # ── world server ─────────────────────────────────────────────────────

    def _serve_world(self, sock):
        with self._lock:
            self.world_connections += 1
        seed = os.urandom(4)
        # AuthChallenge::Write: uint32 1, 4-byte challenge, 32-byte DoS challenge (unencrypted).
        sock.sendall(bd.server_packet(op.SMSG_AUTH_CHALLENGE, struct.pack('<I', 1) + seed + os.urandom(32)))
        # CMSG_AUTH_SESSION: unencrypted client header (BE16 size incl. 4 opcode bytes, LE32 opcode).
        hdr = _recvn(sock, 6)
        body = _recvn(sock, struct.unpack('>H', hdr[:2])[0] - 4)
        assert struct.unpack('<I', hdr[2:])[0] == op.CMSG_AUTH_SESSION
        end = body.index(0, 8)
        account = body[8:end].decode('ascii')
        local_challenge = body[end + 5:end + 9]
        digest = body[end + 9 + 4 + 4 + 4 + 8:][:20]
        key = self.session_keys.get(account)
        if key is None or pk.sha1(account.encode(), b'\0' * 4, local_challenge, seed, key) != digest:
            sock.sendall(bd.server_packet(op.SMSG_AUTH_RESPONSE, bytes([0x0D])))    # AUTH_FAILED
            return
        crypt = cr.WorldCrypt(key)
        # Server -> client uses the agent's recv stream, client -> server its send stream.
        enc, dec = crypt._recv.crypt, crypt._send.crypt
        plan = self._plans.get(account.upper(), NO_FAULTS)
        sent = 0

        def send(opcode: int, payload: bytes = b''):
            nonlocal sent
            sent += 1
            if sent == plan.stall_at:
                time.sleep(plan.stall)
            if opcode == plan.corrupt_opcode:
                payload = b'\xff' * len(payload)
            h = bd.tc_server_header(len(payload) + 2, opcode)
            if not 0 < plan.bad_crypt_from <= sent:
                h = enc(h)
            data = h + payload
            last = sent == plan.drop_after
            _write(sock, data[:len(data) // 2] if last and plan.truncate else data, plan)
            if last:
                if plan.reset:   # SO_LINGER {on, 0 s}: close() sends RST, not FIN
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                raise ConnectionAbortedError("fault plan: drop")

        # WorldSession::SendAuthResponse(AUTH_OK, shortForm): code, billing u32, flags u8, u32, expansion.
        send(op.SMSG_AUTH_RESPONSE, struct.pack('<BIBIB', 0x0C, 0, 0, 0, 2))
        send(op.SMSG_TIME_SYNC_REQ, struct.pack('<I', 0))
        send(op.SMSG_TUTORIAL_FLAGS, b'\0' * 32)          # payload content is not read by the agent

        while True:
            h = dec(_recvn(sock, 6))
            size, opcode = struct.unpack('>H', h[:2])[0], struct.unpack('<I', h[2:])[0]
            payload = _recvn(sock, size - 4)
            with self._lock:
                self.received.append((opcode, payload))
            self._on_packet(send, opcode, payload)

    def _on_packet(self, send, opcode: int, payload: bytes):
        if opcode == op.CMSG_CHAR_ENUM:
            send(op.SMSG_CHAR_ENUM, (FIXTURES / "char_enum" / "three_characters.bin").read_bytes())
        elif opcode == op.CMSG_PLAYER_LOGIN:
            self._enter_world(send)
        elif opcode == op.CMSG_QUEST_QUERY and pk.u32(payload, 0) == QUEST_ID:
            send(op.SMSG_QUEST_QUERY_RESPONSE, (FIXTURES / "quests" / "quest_query_response_8325.bin").read_bytes())
        elif opcode == op.CMSG_QUESTGIVER_COMPLETE_QUEST:
            guid, quest = struct.unpack_from('<QI', payload)
            if (guid, quest) == (QUESTGIVER_GUID, QUEST_ID):
                send(op.SMSG_QUESTGIVER_OFFER_REWARD, bd.offer_reward_payload(
                    guid, quest, QUEST_TITLE, "Well done.", choices=REWARD_CHOICES, money=REWARD_MONEY))
        elif opcode == op.CMSG_QUESTGIVER_CHOOSE_REWARD:
            guid, quest, _choice = struct.unpack_from('<QII', payload)
            if (guid, quest) == (QUESTGIVER_GUID, QUEST_ID):
                send(op.SMSG_QUESTGIVER_QUEST_COMPLETE, bd.quest_complete_payload(quest, money=REWARD_MONEY))
                slot = uf.PLAYER_QUEST_LOG_1_1
                send(op.SMSG_UPDATE_OBJECT, bd.update_object(bd.values_block(
                    SELF_GUID, bd.values_body({slot: 0, slot + 1: 0}))))
        elif opcode == op.CMSG_PING:
            send(op.SMSG_PONG, payload[:4])      # HandlePing: SMSG_PONG echoes the uint32 ping id
        elif opcode == op.CMSG_LOGOUT_REQUEST:
            send(op.SMSG_LOGOUT_COMPLETE)

    def _enter_world(self, send):
        send(op.SMSG_LOGIN_VERIFY_WORLD, struct.pack('<I4f', MAP_ID, *SELF_POS))
        send(op.SMSG_UPDATE_OBJECT, (FIXTURES / "update_object" / "login_self_create.bin").read_bytes())
        x, y, z, o = SELF_POS
        npc_values = bd.values_body({
            uf.OBJECT_FIELD_ENTRY: QUESTGIVER_ENTRY, uf.UNIT_FIELD_LEVEL: 5,
            uf.UNIT_FIELD_HEALTH: 100, uf.UNIT_FIELD_MAXHEALTH: 100,
            uf.UNIT_NPC_FLAGS: UNIT_NPC_FLAG_QUESTGIVER})
        send(op.SMSG_UPDATE_OBJECT, bd.update_object(bd.object_block(
            uo.UPDATETYPE_CREATE_OBJECT2, QUESTGIVER_GUID, uo.TYPEID_UNIT,
            bd.stationary_movement(x + 2.0, y, z, o), npc_values)))
        slot = uf.PLAYER_QUEST_LOG_1_1
        send(op.SMSG_UPDATE_OBJECT, bd.update_object(bd.values_block(
            SELF_GUID, bd.values_body({slot: QUEST_ID, slot + 1: qu.QUEST_STATE_COMPLETE}))))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    faults = FaultPlan.parse(os.environ.get("MOCK_FAULTS", ""))
    if os.environ.get("MOCK_FAULTS_LOOP"):
        faults = itertools.cycle(faults)
    mock = WorldMock(os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("AUTH_PORT", "13724")),
                     int(os.environ.get("WORLD_PORT", "18085")),
                     os.environ.get("MOCK_ACCOUNT", DEFAULT_ACCOUNT),
                     os.environ.get("MOCK_PASSWORD", DEFAULT_PASSWORD), faults=faults)
    log.info("world-mock: auth %s:%d, world %s:%d", mock.host, mock.auth_port, mock.host, mock.world_port)
    threading.Event().wait()


if __name__ == "__main__":
    main()
