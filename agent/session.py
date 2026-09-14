#!/usr/bin/env python3
"""WoW session manager — login, keepalive, packet dispatch.
Wraps the low-level wow_client protocol in a background recv loop."""

import socket
import struct
import time
import threading
import zlib

from . import packets as pk
from . import crypt as cr
from . import perception as per

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

SMSG_UPDATE_OBJECT      = 0x1F7
SMSG_COMPRESSED_UPDATE_OBJECT = 0x1F6
SMSG_LOGOUT_RESPONSE    = 0x04D


class WoWSession:
    """Manages a single character's World of Warcraft session."""

    def __init__(self, host: str, port: int, account_name: str,
                 session_key: bytes, realm_id: int, verbose_packets: bool = False):
        self.host = host
        self.port = port
        self.account_name = account_name
        self.session_key = session_key
        self.realm_id = realm_id
        self.verbose_packets = verbose_packets
        self.sock = None
        self.crypt = None
        self._recv_thread = None
        self._running = False
        self._lock = threading.Lock()

        # Game state
        self.player_guid = 0
        self.player_name = ""
        self.player_position = None  # (map_id, x, y, z, orient)
        self.level = 0
        self.world_state = per.WorldState()  # nearby objects, players, etc.
        self.on_update_object = None  # callback(update_type, guid, fields)

    def connect(self):
        """Connect to world server and authenticate."""
        self.sock = socket.create_connection((self.host, self.port), 15)
        self.sock.settimeout(30)

        # Read SMSG_AUTH_CHALLENGE (first packet, unencrypted)
        hdr = self._rr(4)
        size = struct.unpack('>H', hdr[:2])[0]
        opcode = struct.unpack('<H', hdr[2:4])[0]
        assert opcode == SMSG_AUTH_CHALLENGE
        payload = self._rr(size - 2)
        server_challenge = payload[4:8]

        # Build CMSG_AUTH_SESSION
        import os, hashlib, hmac
        local_challenge = os.urandom(4)
        digest = pk.sha1(self.account_name.encode(), b'\x00'*4, local_challenge,
                          server_challenge, self.session_key)
        pl = struct.pack('<I', 12340) + struct.pack('<I', 0)
        pl += self.account_name.encode('ascii') + b'\x00'
        pl += struct.pack('<I', 0)  # LoginServerType
        pl += local_challenge
        pl += struct.pack('<I', 1)  # RegionID
        pl += struct.pack('<I', 0)  # BattlegroupID
        pl += struct.pack('<I', self.realm_id)
        pl += struct.pack('<Q', 0)  # DosResponse
        pl += digest
        pl += struct.pack('<I', 0) + b'\x00'  # addon info

        # Send unencrypted CMSG_AUTH_SESSION
        self.sock.sendall(
            struct.pack('>H', len(pl) + 4) + struct.pack('<I', CMSG_AUTH_SESSION) + pl
        )

        # Init crypto
        self.crypt = cr.WorldCrypt(self.session_key)

        # Read auth response
        opcode, payload = self._recv_packet()
        assert opcode == SMSG_AUTH_RESPONSE
        result = payload[0]
        if result != 0x0C:
            raise RuntimeError(f"World auth failed: {result:#x}")

        # Drain pre-login packets
        while True:
            opcode, payload = self._recv_packet()
            if opcode == SMSG_TIME_SYNC_REQ:
                self._send_sync(payload)
            elif opcode == SMSG_TUTORIAL_FLAGS:
                break

        return True

    def enum_characters(self) -> list[dict]:
        """List characters on the realm. Returns [{'guid':..., 'name':...}, ...]."""
        self._send_packet(CMSG_CHAR_ENUM)
        opcode, payload = self._recv_packet()
        assert opcode == SMSG_CHAR_ENUM

        count = payload[0]
        off = 1
        chars = []
        for _ in range(count):
            guid = struct.unpack_from('<Q', payload, off)[0]; off += 8
            name = payload[off:payload.index(0, off)].decode(); off += len(name) + 1
            race = payload[off]; cls = payload[off+1]; gender = payload[off+2]
            off += 3 + 5  # gender + cosmetics
            level = payload[off]; off += 1
            off += 4  # zone
            map_id = struct.unpack_from('<I', payload, off)[0]; off += 4
            x = struct.unpack_from('<f', payload, off)[0]; off += 4
            y = struct.unpack_from('<f', payload, off)[0]; off += 4
            z = struct.unpack_from('<f', payload, off)[0]; off += 4
            off += 4  # guild
            char_flags = struct.unpack_from('<I', payload, off)[0]; off += 4
            if char_flags & 0x08: off += 4
            off += 4 + 12 + 23*4  # rest
            chars.append({
                'guid': guid, 'name': name, 'race': race, 'class_': cls,
                'gender': gender, 'level': level, 'map': map_id,
                'x': x, 'y': y, 'z': z,
            })
        return chars

    def login_character(self, guid: int):
        """Log in a character and start background recv."""
        self._send_packet(CMSG_KEEP_ALIVE)
        self._send_packet(CMSG_PLAYER_LOGIN, struct.pack('<Q', guid))

        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            self.sock.settimeout(3)
            try:
                opcode, payload = self._recv_packet()
            except (socket.timeout, TimeoutError):
                self._send_packet(CMSG_KEEP_ALIVE)
                self._send_packet(CMSG_PING, struct.pack('<II', 0, 0))
                continue

            if opcode == SMSG_LOGIN_VERIFY_WORLD:
                self._handle_verify_world(payload)
                self._running = True
                self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
                self._recv_thread.start()
                return True
            elif opcode == SMSG_TIME_SYNC_REQ:
                self._send_sync(payload)
            elif opcode == SMSG_COMPRESSED_UPDATE_OBJECT:
                self._handle_compressed(payload)
            else:
                if self.verbose_packets:
                    print(f"[session] pre-login: {opcode:#05x} ({len(payload)} B)")

        raise TimeoutError("Login timed out")

    def logout(self):
        """Graceful logout."""
        if not self._running:
            return
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=5)
        self._send_packet(CMSG_LOGOUT_REQUEST)
        # Read logout response
        try:
            self.sock.settimeout(5)
            while True:
                opcode, payload = self._recv_packet()
                if opcode == SMSG_LOGOUT_RESPONSE:
                    break
        except Exception:
            pass
        self.sock.close()

    def send_chat(self, message: str, channel: str = "say"):
        """Send a chat message. Channel: 'say', 'yell', 'whisper'."""
        from . import actions
        actions.send_chat_message(self, message, channel)

    # ── Internal ──────────────────────────────────────────────

    def _handle_verify_world(self, payload):
        off = 0
        map_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        px = struct.unpack_from('<f', payload, off)[0]; off += 4
        py = struct.unpack_from('<f', payload, off)[0]; off += 4
        pz = struct.unpack_from('<f', payload, off)[0]; off += 4
        orient = struct.unpack_from('<f', payload, off)[0]
        self.player_position = (map_id, px, py, pz, orient)

    def _handle_compressed(self, payload):
        unc_size = struct.unpack_from('<I', payload, 0)[0]
        inflated = zlib.decompress(payload[4:])
        self._parse_update_object(inflated)

    def _parse_update_object(self, data: bytes):
        """Parse SMSG_UPDATE_OBJECT payload into world state."""
        off = 0
        update_count = struct.unpack_from('<I', data, off)[0]; off += 4
        # fields: uint8 updateType, packed GUID, updateMask, values, movement
        for _ in range(update_count):
            if off >= len(data):
                break
            update_type = data[off]; off += 1
            if update_type in (0, 1, 2, 3):  # OBJECT, MOVEMENT, CREATE_OBJECT, CREATE_OBJECT2
                # packed guid
                mask = data[off]; off += 1
                guid_bytes = []
                for i in range(8):
                    if mask & (1 << i):
                        guid_bytes.append(data[off]); off += 1
                guid = 0
                for b in guid_bytes:
                    guid = guid << 8 | b
            else:
                # OUT_OF_RANGE or NEAR_OBJECTS — skip
                if update_type == 4:  # OUT_OF_RANGE
                    mask = data[off]; off += 1
                    for i in range(8):
                        if mask & (1 << i):
                            off += 1
                continue

            self.world_state.record_guid(guid, update_type)
            # Simple approach: skip rest of update fields for now
            # In a full implementation, parse updateFlags, mask, values
            # For now, just record the GUID

    def _send_sync(self, payload):
        counter = struct.unpack_from('<I', payload, 0)[0]
        self._send_packet(CMSG_TIME_SYNC_RESP, struct.pack('<II', counter, 0))

    def _recv_loop(self):
        """Background thread: read packets and dispatch."""
        last_keepalive = time.monotonic()
        last_sync = time.monotonic()
        while self._running:
            self.sock.settimeout(0.5)
            try:
                opcode, payload = self._recv_packet()
            except (socket.timeout, TimeoutError):
                now = time.monotonic()
                if now - last_keepalive > 15:
                    self._send_packet(CMSG_KEEP_ALIVE)
                    last_keepalive = now
                continue
            except ConnectionError:
                self._running = False
                break

            if opcode == SMSG_TIME_SYNC_REQ:
                self._send_sync(payload)
                last_sync = time.monotonic()
            elif opcode == SMSG_COMPRESSED_UPDATE_OBJECT:
                self._handle_compressed(payload)
            elif opcode == SMSG_UPDATE_OBJECT:
                self._parse_update_object(payload)
            elif opcode == SMSG_PONG:
                pass
            elif opcode == 0x345:  # SMSG_MONSTER_MOVE
                pass
            elif opcode == 0x0DD:  # SMSG_STAND_STATE_UPDATE
                pass
            elif opcode == SMSG_LOGOUT_RESPONSE:
                self._running = False
            else:
                # print(f"[session] {opcode:#05x} ({len(payload)} B)")
                pass

    def _send_packet(self, opcode: int, payload: bytes = b''):
        hdr = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        if self.crypt:
            hdr = self.crypt.encrypt_send(hdr)
        with self._lock:
            self.sock.sendall(hdr + payload)

    def _recv_packet(self) -> tuple[int, bytes]:
        hdr = self._rr(4)
        if self.crypt:
            hdr = self.crypt.decrypt_recv(hdr)
        size = struct.unpack('>H', hdr[:2])[0]
        opcode = struct.unpack('<H', hdr[2:4])[0]
        # Large packet
        if size & 0x8000:
            extra = self._rr(1)
            if self.crypt:
                extra = self.crypt.decrypt_recv(extra)
            size = ((size & 0x7FFF) << 8) | extra[0]
            opcode = struct.unpack('<H', hdr[2:4] + extra)[0]
        plen = max(0, size - 2)
        return opcode, self._rr(plen) if plen > 0 else b''

    def _rr(self, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("disconnected")
            buf += c
        return buf