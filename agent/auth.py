#!/usr/bin/env python3
"""SRP6 auth client (port 3724) for WoW 3.3.5a / TrinityCore."""

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


# AuthResult (TrinityCore 3.3.5 AuthCodes.h) as named in an error message.
AUTH_RESULT_NAMES = {
    0x03: "banned", 0x04: "unknown account or wrong password", 0x05: "incorrect password",
    0x06: "account already online", 0x08: "auth database busy", 0x09: "client version invalid",
    0x0A: "client version update required", 0x0C: "suspended (temporary ban)", 0x0D: "no access",
    0x10: "account locked to another IP", 0x19: "account locked to another country",
}

# #430: refusals a retry cannot fix (banned, unknown account or wrong password, bad client
# version, no access). Every other AuthResult is transient and the supervisor retries it.
PERMANENT_AUTH_CODES = frozenset({0x03, 0x04, 0x05, 0x09, 0x0A, 0x0D})


class AuthRejected(RuntimeError):
    """The auth server answered a logon step with a non-zero AuthResult. `code` is that
    byte; the message names the step and the result, never a credential or key."""

    def __init__(self, step: str, code: int):
        self.code = code
        name = AUTH_RESULT_NAMES.get(code, "unknown result")
        super().__init__(f"auth server rejected the {step}: {name} (AuthResult 0x{code:02X})")


def _reply(sock: socket.socket, cmd: int, error_at: int, rest: int, step: str) -> bytes:
    """One auth reply. TrinityCore (AuthSession.cpp) sends only the bytes up to and including
    the AuthResult when it fails (challenge: cmd, 0, result = 3; proof: cmd, result, ...) and
    leaves the socket open, so read up to the result byte, branch, then read the other `rest`."""
    head = _recvn(sock, error_at + 1)
    if head[0] != cmd:
        raise pk.ProtocolError(f"auth {step}: expected command 0x{cmd:02X}, got 0x{head[0]:02X}")
    if head[error_at] != AUTH_RESULT_SUCCESS:
        raise AuthRejected(step, head[error_at])
    return head + _recvn(sock, rest)


def auth_logon(host: str, port: int, username: str, password: str):
    """Auth handshake. Returns (account_name, session_key, realm_info).
    Raises AuthRejected when the server refuses the account or the password."""
    with socket.create_connection((host, port), timeout=10) as sock:
        return _handshake(sock, username, password)


def _handshake(sock: socket.socket, username: str, password: str):
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

    # Success is 119 bytes (3 + B 32 + g 2 + N 33 + s 32 + crc 16 + security flags 1).
    resp = _reply(sock, AUTH_CMD_LOGON_CHALLENGE, 2, 116, "logon challenge")
    off = 3
    B = resp[off:off+32]; off += 32
    g_len = pk.u8(resp, off); off += 1
    _g_val = resp[off:off+g_len]; off += g_len
    n_len = pk.u8(resp, off); off += 1
    N = resp[off:off+n_len]; off += n_len
    s = resp[off:off+32]; off += 32
    _crc_srv = resp[off:off+16]; off += 16
    security_flags = pk.u8(resp, off); off += 1

    A, M1, crc_hash, session_key = compute_srp6(username, password, s, B, N)

    proof = pk.p8(AUTH_CMD_LOGON_PROOF) + A + M1 + crc_hash
    proof += pk.p8(0) + pk.p8(security_flags)
    sock.sendall(proof)

    # A wrong password is 4 bytes (cmd, result, uint16 0); success is 32 (M2 + account flags ...).
    _reply(sock, AUTH_CMD_LOGON_PROOF, 1, 30, "logon proof")

    # Realm list
    sock.sendall(pk.p8(AUTH_CMD_REALM_LIST) + pk.p32(BUILD))
    hdr = _recvn(sock, 3)
    rcmd = pk.u8(hdr, 0)
    rsize = pk.u16(hdr, 1)
    if rcmd != AUTH_CMD_REALM_LIST:
        raise pk.ProtocolError(f"auth realm list: expected command 0x10, got 0x{rcmd:02X}")
    body = _recvn(sock, rsize)

    off = 0
    _unk = pk.u32(body, off); off += 4
    realm_count = pk.u16(body, off); off += 2
    realm_info = {}
    for i in range(realm_count):
        _rtype = pk.u8(body, off); off += 1
        _locked = pk.u8(body, off); off += 1
        rflags = pk.u8(body, off); off += 1
        rname, off = pk.cstring(body, off)
        raddr, off = pk.cstring(body, off)
        _rpop = pk.f32(body, off); off += 4
        _rchars = pk.u8(body, off); off += 1
        _rtz = pk.u8(body, off); off += 1
        rid = pk.u8(body, off); off += 1
        if rflags & 0x04:
            off += 5  # build info
        realm_info[rid] = {'name': rname, 'address': raddr, 'id': rid}

    return username.upper(), session_key, realm_info


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("disconnected")
        buf += c
    return buf