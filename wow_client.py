#!/usr/bin/env python3
"""Headless WoW 3.3.5a agent client — pure Python, stdlib only.
Logs into a TrinityCore server via SRP6 auth, enumerates characters,
and enters the game world as a character."""

import hashlib
import os
import socket
import struct
import sys
import time


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

BUILD = 12340
AUTH_CMD_LOGON_CHALLENGE = 0x00
AUTH_CMD_LOGON_PROOF     = 0x01
AUTH_CMD_REALM_LIST      = 0x10
AUTH_RESULT_SUCCESS      = 0x00
AUTH_OK                  = 0x0C  # SMSG_AUTH_RESPONSE success

VERSION_CHALLENGE = bytes([
    0xBA, 0xA3, 0x1E, 0x99, 0xA0, 0x0B, 0x21, 0x57,
    0xFC, 0x37, 0x3F, 0xB3, 0x69, 0xCD, 0xD2, 0xF1,
])

# Opcodes
SMSG_AUTH_CHALLENGE     = 0x1EC
CMSG_AUTH_SESSION       = 0x1ED
SMSG_AUTH_RESPONSE      = 0x1EE
CMSG_CHAR_ENUM          = 0x037
SMSG_CHAR_ENUM          = 0x03B
CMSG_PLAYER_LOGIN       = 0x03D
CMSG_LOGOUT_REQUEST     = 0x04B
CMSG_PING               = 0x1DC
SMSG_PONG               = 0x1DD
SMSG_LOGIN_VERIFY_WORLD = 0x236
SMSG_ADDON_INFO         = 0x2EF
SMSG_TIME_SYNC_REQ      = 0x390
CMSG_TIME_SYNC_RESP     = 0x391
CMSG_KEEP_ALIVE         = 0x407
SMSG_TUTORIAL_FLAGS     = 0x0FD


# ═══════════════════════════════════════════════════════════════
# Binary helpers
# ═══════════════════════════════════════════════════════════════

def le_u8(data: bytes, off: int) -> int:
    return data[off]

def le_u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]

def le_u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]

def le_u64(data: bytes, off: int) -> int:
    return struct.unpack_from('<Q', data, off)[0]

def le_f32(data: bytes, off: int) -> float:
    return struct.unpack_from('<f', data, off)[0]

def pack_u8(v: int) -> bytes:
    return struct.pack('<B', v)

def pack_u16(v: int) -> bytes:
    return struct.pack('<H', v)

def pack_u32(v: int) -> bytes:
    return struct.pack('<I', v)

def pack_u64(v: int) -> bytes:
    return struct.pack('<Q', v)

def pack_f32(v: float) -> bytes:
    return struct.pack('<f', v)

def read_cstring(data: bytes, off: int) -> tuple[str, int]:
    end = data.index(0, off)
    return data[off:end].decode('ascii', errors='replace'), end + 1


# ═══════════════════════════════════════════════════════════════
# RC4
# ═══════════════════════════════════════════════════════════════

class RC4:
    def __init__(self, key: bytes):
        self._key = bytearray(key)
        self._init()

    def _init(self):
        key_len = len(self._key)
        self._state = bytearray(range(256))
        j = 0
        for i in range(256):
            j = (j + self._state[i] + self._key[i % key_len]) % 256
            self._state[i], self._state[j] = self._state[j], self._state[i]
        self._i = 0
        self._j = 0

    def drop(self, n: int):
        for _ in range(n):
            self._next()

    def _next(self) -> int:
        self._i = (self._i + 1) % 256
        self._j = (self._j + self._state[self._i]) % 256
        self._state[self._i], self._state[self._j] = (
            self._state[self._j], self._state[self._i])
        return self._state[
            (self._state[self._i] + self._state[self._j]) % 256
        ]

    def crypt(self, data: bytes) -> bytes:
        return bytes(b ^ self._next() for b in data)


class WorldCrypt:
    """WotLK header crypt, matching TrinityCore's WorldPacketCrypt:
    RC4 keys are HMAC-SHA1(magic_key, session_key), 1024-byte drop, header-only."""

    SERVER_ENCRYPTION_KEY = bytes([
        0xCC, 0x98, 0xAE, 0x04, 0xE8, 0x97, 0xEA, 0xCA,
        0x12, 0xDD, 0xC0, 0x93, 0x42, 0x91, 0x53, 0x57,
    ])
    SERVER_DECRYPTION_KEY = bytes([
        0xC2, 0xB3, 0x72, 0x3C, 0xC6, 0xAE, 0xD9, 0xB5,
        0x34, 0x3C, 0x53, 0xEE, 0x2F, 0x43, 0x67, 0xCE,
    ])

    def __init__(self, session_key: bytes):
        import hmac as _hmac
        assert len(session_key) == 40
        recv_key = _hmac.new(self.SERVER_ENCRYPTION_KEY, session_key, hashlib.sha1).digest()
        send_key = _hmac.new(self.SERVER_DECRYPTION_KEY, session_key, hashlib.sha1).digest()
        self._send = RC4(send_key)
        self._recv = RC4(recv_key)
        self._send.drop(1024)
        self._recv.drop(1024)

    def decrypt_recv(self, data: bytes) -> bytes:
        return self._recv.crypt(data)

    def encrypt_send(self, data: bytes) -> bytes:
        return self._send.crypt(data)


# ═══════════════════════════════════════════════════════════════
# Network helpers
# ═══════════════════════════════════════════════════════════════

def recvn(sock: socket.socket, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("disconnected")
        buf += chunk
    return buf


# ═══════════════════════════════════════════════════════════════
# SRP6 client
# ═══════════════════════════════════════════════════════════════

def _mpi_be(b: bytes) -> int:
    return int.from_bytes(b, 'big')

def _mpi_le(b: bytes) -> int:
    return int.from_bytes(b, 'little')

def sha1(*parts: bytes) -> bytes:
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()

def sha1_interleave(s_32: bytes) -> bytes:
    """Session key from S — skipping leading zero bytes per TrinityCore SRP6."""
    assert len(s_32) == 32
    p = 0
    while p < 32 and s_32[p] == 0:
        p += 1
    if p & 1:
        p += 1
    p //= 2
    half0 = bytes(s_32[i * 2] for i in range(p, 16))
    half1 = bytes(s_32[i * 2 + 1] for i in range(p, 16))
    h0 = sha1(half0)
    h1 = sha1(half1)
    return bytes(b for i in range(20) for b in (h0[i], h1[i]))


def compute_srp6(username: str, password: str, s_le: bytes, B_le: bytes, N_le: bytes):
    """Client-side SRP6.
    All byte representations are little-endian on the wire (old BigNumber),
    but M1/M2 hashing uses big-endian representation.
    Returns (A_le, M1, crc_hash, session_key).
    """
    u_upper = username.upper().encode('ascii')
    p_bytes = password.upper().encode('ascii')

    # Numeric values (LE interpretation from wire bytes)
    N_int = _mpi_le(N_le)
    g_int = 7
    B_int = _mpi_le(B_le)
    s_int = _mpi_le(s_le)

    # x = SHA1(s || SHA1(username_upper || ":" || password))
    inner = sha1(u_upper + b':' + p_bytes)
    x_bytes = sha1(s_le + inner)
    x_int = _mpi_le(x_bytes)

    # a = random ~19 bytes, A = g^a mod N
    a_int = _mpi_le(os.urandom(19)) % N_int
    A_int = pow(g_int, a_int, N_int)
    A_le = A_int.to_bytes(32, 'little')
    A_be = A_int.to_bytes(32, 'big')

    # u = SHA1(A_le || B_le) interpreted as LE int (TrinityCore 3.3.5 old: SHA1 of wire bytes)
    u_int = _mpi_le(sha1(A_le + B_le))

    # S = (B - k*g^x)^(a + u*x) mod N, k=3
    # NOTE: exponent must NOT be reduced mod N (group order is N-1, not N)
    gx = pow(g_int, x_int, N_int)
    base = (B_int - 3 * gx) % N_int
    exp = a_int + u_int * x_int
    S_int = pow(base, exp, N_int)
    S_le = S_int.to_bytes(32, 'little')

    session_key = sha1_interleave(S_le)

    # M1 = SHA1(NgHash || H(I) || s_le || A_le || B_le || session_key)
    # All bytes are in wire (little-endian) format — matching TrinityCore 3.3.5
    N_le_wire = N_le  # received on wire
    g_be = b'\x07'
    NgHash = bytes(a ^ b for a, b in zip(sha1(N_le_wire), sha1(g_be)))
    H_I = sha1(u_upper)

    M1 = sha1(NgHash + H_I + s_le + A_le + B_le + session_key)

    # crc_hash = SHA1(A_le || VersionChallenge)
    crc_hash = sha1(A_le + VERSION_CHALLENGE)

    return A_le, M1, crc_hash, session_key


# ═══════════════════════════════════════════════════════════════
# Auth (port 3724)
# ═══════════════════════════════════════════════════════════════

def auth_logon(host: str, port: int, username: str, password: str) -> tuple:
    sock = socket.create_connection((host, port), timeout=10)

    # 1. Send logon challenge
    u_upper = username.upper().encode('ascii')
    cmd = pack_u8(AUTH_CMD_LOGON_CHALLENGE)
    error = pack_u8(0)
    size = pack_u16(30 + len(u_upper))
    gamename = b'WoW\x00'
    ver = struct.pack('<BBB', 3, 3, 5)
    build = pack_u16(BUILD)
    platform = b'68x\x00'
    os_str = b'niW\x00'
    country = b'enUS'
    tz = pack_u32(0)
    ip = pack_u32(0)
    i_len = pack_u8(len(u_upper))
    pkt = cmd + error + size + gamename + ver + build + platform + os_str + country + tz + ip + i_len + u_upper
    sock.sendall(pkt)

    # 2. Read challenge response: cmd(1) + unused(1) + result(1) + B[32] + g_len(1) + g[1] + N_len(1) + N[32] + s[32] + crc[16] + flags(1)
    resp = recvn(sock, 119)
    off = 0
    cmd_b = le_u8(resp, off); off += 1
    unused = le_u8(resp, off); off += 1
    result = le_u8(resp, off); off += 1
    assert cmd_b == AUTH_CMD_LOGON_CHALLENGE
    assert unused == 0
    if result != AUTH_RESULT_SUCCESS:
        print(f"[auth] logon challenge failed: {result:#04x}")
        sock.close()
        return None, None, None

    B = resp[off:off+32]; off += 32
    g_len = le_u8(resp, off); off += 1
    g_val = resp[off:off+g_len]; off += g_len
    N_len = le_u8(resp, off); off += 1
    N = resp[off:off+N_len]; off += N_len
    s = resp[off:off+32]; off += 32
    version_challenge_srv = resp[off:off+16]; off += 16
    security_flags = le_u8(resp, off); off += 1

    print(f"[auth] challenge accepted. g={g_val.hex()} N={N.hex()[:20]}... s={s.hex()[:20]}...")

    # 3. Compute SRP6 proof
    A, M1, crc_hash, session_key = compute_srp6(username, password, s, B, N)
    print(f"[auth] A={A.hex()[:20]}... M1={M1.hex()}")

    # 4. Send logon proof
    proof = pack_u8(AUTH_CMD_LOGON_PROOF) + A + M1 + crc_hash + pack_u8(0) + pack_u8(security_flags)
    sock.sendall(proof)

    # 5. Read proof response
    proof_resp = recvn(sock, 2)
    cmd2 = le_u8(proof_resp, 0)
    err2 = le_u8(proof_resp, 1)
    assert cmd2 == AUTH_CMD_LOGON_PROOF
    if err2 != 0:
        rest = recvn(sock, 2)
        print(f"[auth] logon proof failed: {err2:#04x}")
        sock.close()
        return None, None, None

    more = recvn(sock, 30)
    proof_resp = proof_resp + more
    off2 = 2
    M2_srv = proof_resp[off2:off2+20]; off2 += 20
    account_flags = le_u32(proof_resp, off2); off2 += 4
    survey_id = le_u32(proof_resp, off2); off2 += 4
    login_flags = le_u16(proof_resp, off2); off2 += 2

    expected_M2 = sha1(A + M1 + session_key)
    if M2_srv != expected_M2:
        print(f"[auth] WARNING: server M2 mismatch — StrictVersionCheck likely off")
    else:
        print(f"[auth] M2 verified. account_flags={account_flags:#x}")

    # 6. Request realm list
    realm_req = pack_u8(AUTH_CMD_REALM_LIST) + pack_u32(BUILD)
    sock.sendall(realm_req)

    hdr = recvn(sock, 3)
    rcmd = le_u8(hdr, 0)
    rsize = le_u16(hdr, 1)
    assert rcmd == AUTH_CMD_REALM_LIST
    body = recvn(sock, rsize)
    
    off = 0
    unk = le_u32(body, off); off += 4
    realm_count = le_u16(body, off); off += 2
    print(f"[auth] realms: {realm_count}")

    realm_info = {}
    for i in range(realm_count):
        rtype = le_u8(body, off); off += 1
        locked = le_u8(body, off); off += 1
        rflags = le_u8(body, off); off += 1
        rname, off = read_cstring(body, off)
        raddr, off = read_cstring(body, off)
        rpop = le_f32(body, off); off += 4
        rchars = le_u8(body, off); off += 1
        rtz = le_u8(body, off); off += 1
        rid = le_u8(body, off); off += 1
        if rflags & 0x04:
            off += 1+1+1+2  # build info
        print(f"  realm {rid}: {rname} @ {raddr} pop={rpop} chars={rchars}")
        realm_info[rid] = {'name': rname, 'address': raddr, 'id': rid}

    sock.close()
    return username.upper(), session_key, realm_info


# ═══════════════════════════════════════════════════════════════
# World (port 8085)
# ═══════════════════════════════════════════════════════════════

def world_login(host: str, port: int, account_name: str, session_key: bytes,
                realm_id: int, character_guid: int, stay_seconds: float = 10.0):
    sock = socket.create_connection((host, port), timeout=15)
    crypt = None

    def send_packet(opcode: int, payload: bytes = b''):
        header = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        if crypt:
            header = crypt.encrypt_send(header)
        sock.sendall(header + payload)

    def recv_packet():
        hdr = recvn(sock, 4)
        if crypt:
            hdr = crypt.decrypt_recv(hdr)
        size = struct.unpack('>H', hdr[:2])[0]
        opcode = struct.unpack('<H', hdr[2:4])[0]
        # Large packet: 3-byte size with 0x80 flag set on first byte
        if size & 0x8000:
            extra = recvn(sock, 1)
            if crypt:
                extra = crypt.decrypt_recv(extra)
            size = ((size & 0x7FFF) << 8) | extra[0]
            opcode = struct.unpack('<H', hdr[2:4] + extra)[0]
        plen = max(0, size - 2)
        payload = recvn(sock, plen) if plen > 0 else b''
        return opcode, payload

    # Server sends SMSG_AUTH_CHALLENGE first
    opcode, payload = recv_packet()
    assert opcode == SMSG_AUTH_CHALLENGE, f"expected AUTH_CHALLENGE, got {opcode:#x}"
    off = 0
    dos_zero = le_u32(payload, off); off += 4
    server_challenge = payload[off:off+4]; off += 4
    print(f"[world] AUTH_CHALLENGE: seed={server_challenge.hex()}")

    local_challenge = os.urandom(4)
    digest = sha1(
        account_name.encode('ascii'),
        b'\x00' * 4,
        local_challenge,
        server_challenge,
        session_key,
    )

    # CMSG_AUTH_SESSION
    payload = b''
    payload += pack_u32(BUILD)
    payload += pack_u32(0)  # LoginServerID
    payload += account_name.encode('ascii') + b'\x00'
    payload += pack_u32(0)  # LoginServerType
    payload += local_challenge
    payload += pack_u32(1)  # RegionID
    payload += pack_u32(0)  # BattlegroupID
    payload += pack_u32(realm_id)
    payload += pack_u64(0)  # DosResponse
    payload += digest          # 20 bytes
    payload += pack_u32(0) + b'\x00'  # addon info

    send_packet(CMSG_AUTH_SESSION, payload)

    # Init crypto BEFORE reading the response — server inits crypto before sending ANY response
    crypt = WorldCrypt(session_key)

    # Read SMSG_AUTH_RESPONSE
    opcode, payload = recv_packet()
    assert opcode == SMSG_AUTH_RESPONSE, f"expected AUTH_RESPONSE, got {opcode:#x}"
    result = le_u8(payload, 0)
    print(f"[world] AUTH_RESPONSE: {result:#x}")
    if result != AUTH_OK:
        print(f"[world] auth failed: {result:#x}")
        sock.close()
        return

    # Drain pre-login packets, answering TIME_SYNC_REQ while waiting
    for _ in range(30):
        opcode, payload = recv_packet()
        print(f"[world] recv {opcode:#05x} ({len(payload)} bytes)")
        if opcode == SMSG_TIME_SYNC_REQ:
            counter = le_u32(payload, 0)
            send_packet(CMSG_TIME_SYNC_RESP, pack_u32(counter) + pack_u32(0))
        if opcode == SMSG_TUTORIAL_FLAGS:
            break

    # CMSG_CHAR_ENUM
    send_packet(CMSG_CHAR_ENUM)
    opcode, payload = recv_packet()
    assert opcode == SMSG_CHAR_ENUM, f"expected CHAR_ENUM, got {opcode:#x}"
    char_count = le_u8(payload, 0)
    print(f"[world] characters: {char_count}")
    off = 1
    chars = []
    for i in range(char_count):
        guid = le_u64(payload, off); off += 8
        name, off = read_cstring(payload, off)
        race = le_u8(payload, off); off += 1
        cls = le_u8(payload, off); off += 1
        gender = le_u8(payload, off); off += 1
        off += 5   # skin, face, hair_style, hair_color, facial_hair
        level = le_u8(payload, off); off += 1
        off += 4   # zone
        off += 4   # map
        off += 12  # x,y,z
        off += 4   # guild_id
        char_flags = le_u32(payload, off); off += 4
        if char_flags & 0x00000008:
            off += 4
        off += 4   # first_login
        off += 12  # pet info
        off += 23 * 4  # equipment
        chars.append({'guid': guid, 'name': name, 'race': race, 'class': cls, 'level': level})
        print(f"  [{guid}] {name} L{level} class={cls} race={race}")

    char = next((c for c in chars if c['guid'] == character_guid), None) or (chars[0] if chars else None)
    if not char:
        print("[world] no characters")
        sock.close()
        return

    # CMSG_KEEP_ALIVE — some servers need this before allowing char login
    send_packet(CMSG_KEEP_ALIVE)

    # CMSG_PLAYER_LOGIN
    send_packet(CMSG_PLAYER_LOGIN, pack_u64(char['guid']))

    # Wait for SMSG_LOGIN_VERIFY_WORLD — may be preceded by other packets
    t_logged = time.monotonic()
    while time.monotonic() - t_logged < 30:
        sock.settimeout(5)
        try:
            opcode, payload = recv_packet()
        except (socket.timeout, TimeoutError):
            send_packet(CMSG_KEEP_ALIVE)
            send_packet(CMSG_PING, pack_u32(0) + pack_u32(0))
            continue

        if opcode == SMSG_LOGIN_VERIFY_WORLD:
            off = 0
            map_id = struct.unpack_from('<i', payload, off)[0]; off += 4
            px = le_f32(payload, off); off += 4
            py = le_f32(payload, off); off += 4
            pz = le_f32(payload, off); off += 4
            orient = le_f32(payload, off); off += 4
            print(f"[world] LOGIN_VERIFY_WORLD: map={map_id} pos=({px:.1f},{py:.1f},{pz:.1f})")
            break
        elif opcode == SMSG_TIME_SYNC_REQ:
            counter = le_u32(payload, 0)
            send_packet(CMSG_TIME_SYNC_RESP, pack_u32(counter) + pack_u32(0))
        elif opcode == 0x1F6:  # SMSG_COMPRESSED_UPDATE_OBJECT
            import zlib as _zlib
            unc_size = le_u32(payload, 0)
            inflated = _zlib.decompress(payload[4:])
            print(f"[world] compressed ({len(payload)}->{len(inflated)} bytes)")
        else:
            print(f"[world] recv {opcode:#05x} ({len(payload)} bytes)")

    # Keepalive
    start = time.monotonic()
    print(f"[world] logged in as {char['name']}. keepalive {stay_seconds}s...")
    last_keepalive = 0.0
    while time.monotonic() - start < stay_seconds:
        sock.settimeout(0.5)
        try:
            opcode, payload = recv_packet()
            if opcode == SMSG_TIME_SYNC_REQ:
                counter = le_u32(payload, 0)
                send_packet(CMSG_TIME_SYNC_RESP, pack_u32(counter) + pack_u32(0))
                print(f"[world] time sync: {counter}")
            elif opcode == SMSG_PONG:
                pass
            elif opcode == 0x1F6:  # SMSG_COMPRESSED_UPDATE_OBJECT
                import zlib as _zlib
                # payload: uint32 uncompressed_size, then zlib-compressed data
                unc_size = le_u32(payload, 0)
                inflated = _zlib.decompress(payload[4:])
                print(f"[world] compressed update: {len(payload)}->{len(inflated)} bytes (declared {unc_size})")
            elif opcode == 0x04D:  # SMSG_LOGOUT_RESPONSE
                print("[world] logout response received")
                break
            else:
                print(f"[world] recv {opcode:#05x} ({len(payload)} bytes)")
        except (socket.timeout, TimeoutError):
            now = time.monotonic()
            if now - last_keepalive > 15:
                send_packet(CMSG_KEEP_ALIVE)
                send_packet(CMSG_PING, pack_u32(0) + pack_u32(0))
                last_keepalive = now
        except ConnectionError:
            break

    # Logout
    print("[world] logging out...")
    send_packet(CMSG_LOGOUT_REQUEST)
    sock.settimeout(5)
    try:
        while True:
            opcode, payload = recv_packet()
            print(f"[world] post-logout: {opcode:#05x}")
            if opcode == 0x04D:
                break
    except Exception:
        pass

    sock.close()
    print("[world] done.")


# ═══════════════════════════════════════════════════════════════
# Driver
# ═══════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 wow_client.py <username> <password> [char_guid] [stay_secs]")
        sys.exit(1)

    username = sys.argv[1]
    password = sys.argv[2]
    char_guid = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    stay = float(sys.argv[4]) if len(sys.argv) > 4 else 10.0

    account, session_key, realms = auth_logon("192.168.1.64", 3724, username, password)
    if not account:
        print("AUTH FAILED")
        sys.exit(1)

    realm_ids = list(realms.keys())
    if not realm_ids:
        print("No realms")
        sys.exit(1)
    rid = realm_ids[0]
    realm = realms[rid]
    address = realm['address']
    world_host, world_port_s = address.rsplit(':', 1) if ':' in address else (address, '8085')
    world_port = int(world_port_s)

    print(f"\n[main] realm: {realm['name']} @ {world_host}:{world_port}")
    world_login(world_host, world_port, account, session_key, rid, char_guid, stay)


if __name__ == '__main__':
    main()