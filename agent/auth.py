#!/usr/bin/env python3
"""SRP6 auth client (port 3724) for WoW 3.3.5a / TrinityCore."""

import hashlib
import os
import socket
import struct

from . import packets as pk

AUTH_CMD_LOGON_CHALLENGE = 0x00
AUTH_CMD_LOGON_PROOF     = 0x01
AUTH_CMD_REALM_LIST      = 0x10
AUTH_RESULT_SUCCESS      = 0x00
BUILD = 12340

VERSION_CHALLENGE = bytes([
    0xBA, 0xA3, 0x1E, 0x99, 0xA0, 0x0B, 0x21, 0x57,
    0xFC, 0x37, 0x3F, 0xB3, 0x69, 0xCD, 0xD2, 0xF1,
])


def _mpi_le(b: bytes) -> int:
    return int.from_bytes(b, 'little')


def _sha1_interleave(s_32: bytes) -> bytes:
    """Session key from S (little-endian)."""
    assert len(s_32) == 32
    p = 0
    while p < 32 and s_32[p] == 0:
        p += 1
    if p & 1:
        p += 1
    p //= 2
    h0 = pk.sha1(bytes(s_32[i * 2] for i in range(p, 16)))
    h1 = pk.sha1(bytes(s_32[i * 2 + 1] for i in range(p, 16)))
    return bytes(b for i in range(20) for b in (h0[i], h1[i]))


def compute_srp6(username: str, password: str, s_le: bytes, B_le: bytes,
                 N_le: bytes):
    """Client-side SRP6. Returns (A_le, M1, crc_hash, session_key)."""
    u_upper = username.upper().encode('ascii')
    p_bytes = password.upper().encode('ascii')

    N_int = _mpi_le(N_le)
    B_int = _mpi_le(B_le)

    inner = pk.sha1(u_upper + b':' + p_bytes)
    x_int = _mpi_le(pk.sha1(s_le + inner))

    a_int = _mpi_le(os.urandom(19)) % N_int
    A_int = pow(7, a_int, N_int)
    A_le = A_int.to_bytes(32, 'little')

    u_int = _mpi_le(pk.sha1(A_le + B_le))
    gx = pow(7, x_int, N_int)
    base = (B_int - 3 * gx) % N_int
    exp = a_int + u_int * x_int  # NOT mod N (group order is N-1)
    S_int = pow(base, exp, N_int)
    S_le = S_int.to_bytes(32, 'little')

    session_key = _sha1_interleave(S_le)

    NgHash = bytes(a ^ b for a, b in zip(pk.sha1(N_le), pk.sha1(b'\x07')))
    H_I = pk.sha1(u_upper)
    M1 = pk.sha1(NgHash + H_I + s_le + A_le + B_le + session_key)

    crc_hash = pk.sha1(A_le + VERSION_CHALLENGE)

    return A_le, M1, crc_hash, session_key


def auth_logon(host: str, port: int, username: str, password: str):
    """Auth handshake. Returns (account_name, session_key, realm_info)."""
    sock = socket.create_connection((host, port), timeout=10)

    u_upper = username.upper().encode('ascii')
    pkt = pk.p8(AUTH_CMD_LOGON_CHALLENGE)
    pkt += pk.p8(0)  # error
    pkt += pk.p16(30 + len(u_upper))
    pkt += b'WoW\x00'
    pkt += struct.pack('<BBB', 3, 3, 5)
    pkt += pk.p16(BUILD)
    pkt += b'68x\x00'
    pkt += b'niW\x00'
    pkt += b'enUS'
    pkt += pk.p32(0)  # timezone
    pkt += pk.p32(0)  # ip
    pkt += pk.p8(len(u_upper))
    pkt += u_upper
    sock.sendall(pkt)

    # Challenge response (119 bytes minimum)
    resp = _recvn(sock, 119)
    off = 0
    cmd = pk.u8(resp, off); off += 1
    _unused = pk.u8(resp, off); off += 1
    result = pk.u8(resp, off); off += 1
    assert cmd == AUTH_CMD_LOGON_CHALLENGE
    if result != AUTH_RESULT_SUCCESS:
        sock.close()
        return None, None, None

    B = resp[off:off+32]; off += 32
    g_len = pk.u8(resp, off); off += 1
    g_val = resp[off:off+g_len]; off += g_len
    n_len = pk.u8(resp, off); off += 1
    N = resp[off:off+n_len]; off += n_len
    s = resp[off:off+32]; off += 32
    _crc_srv = resp[off:off+16]; off += 16
    security_flags = pk.u8(resp, off); off += 1

    A, M1, crc_hash, session_key = compute_srp6(username, password, s, B, N)

    proof = pk.p8(AUTH_CMD_LOGON_PROOF) + A + M1 + crc_hash
    proof += pk.p8(0) + pk.p8(security_flags)
    sock.sendall(proof)

    proof_resp = _recvn(sock, 2)
    cmd2 = pk.u8(proof_resp, 0)
    err2 = pk.u8(proof_resp, 1)
    assert cmd2 == AUTH_CMD_LOGON_PROOF
    if err2 != 0:
        sock.close()
        return None, None, None

    more = _recvn(sock, 30)
    proof_resp += more

    # Realm list
    sock.sendall(pk.p8(AUTH_CMD_REALM_LIST) + pk.p32(BUILD))
    hdr = _recvn(sock, 3)
    rcmd = pk.u8(hdr, 0)
    rsize = pk.u16(hdr, 1)
    assert rcmd == AUTH_CMD_REALM_LIST
    body = _recvn(sock, rsize)

    off = 0
    _unk = pk.u32(body, off); off += 4
    realm_count = pk.u16(body, off); off += 2
    realm_info = {}
    for i in range(realm_count):
        rtype = pk.u8(body, off); off += 1
        locked = pk.u8(body, off); off += 1
        rflags = pk.u8(body, off); off += 1
        rname, off = pk.cstring(body, off)
        raddr, off = pk.cstring(body, off)
        rpop = pk.f32(body, off); off += 4
        rchars = pk.u8(body, off); off += 1
        rtz = pk.u8(body, off); off += 1
        rid = pk.u8(body, off); off += 1
        if rflags & 0x04:
            off += 5  # build info
        realm_info[rid] = {'name': rname, 'address': raddr, 'id': rid}

    sock.close()
    return username.upper(), session_key, realm_info


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("disconnected")
        buf += c
    return buf