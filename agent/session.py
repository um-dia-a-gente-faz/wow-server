#!/usr/bin/env python3
"""WoW session manager — login, keepalive, packet dispatch.
Wraps the low-level world protocol in a background recv loop."""

import collections
import logging
import os
import socket
import struct
import time
import threading
import zlib

from . import packets as pk
from . import crypt as cr
from . import perception as per
from . import update_fields as uo_fields
from . import update_object as uo
from .update_object import (
    UPDATETYPE_VALUES,
    UPDATETYPE_MOVEMENT,
    UPDATETYPE_CREATE_OBJECT,
    UPDATETYPE_CREATE_OBJECT2,
    UPDATETYPE_OUT_OF_RANGE_OBJECTS,
    UPDATETYPE_NEAR_OBJECTS,
)

log = logging.getLogger("agent.session")

# Opcodes (TrinityCore 3.3.5 Opcodes.h)
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

SMSG_UPDATE_OBJECT      = 0x0A9
SMSG_DESTROY_OBJECT     = 0x0AA
SMSG_COMPRESSED_UPDATE_OBJECT = 0x1F6
SMSG_LOGOUT_COMPLETE    = 0x04D
SMSG_MONSTER_MOVE       = 0x0DD
SMSG_STANDSTATE_UPDATE  = 0x29D

SMSG_GROUP_INVITE       = 0x06F
SMSG_MESSAGECHAT        = 0x096
SMSG_GM_MESSAGECHAT     = 0x3B3

# ChatMsg values this parser understands by name (SharedDefines.h); anything
# else is kept as "type_<n>" rather than dropped.
CHAT_KIND_NAMES = {
    0x00: "system", 0x01: "say", 0x02: "party", 0x03: "raid", 0x04: "guild",
    0x05: "officer", 0x06: "yell", 0x07: "whisper", 0x08: "whisper_foreign",
    0x09: "whisper_inform", 0x0A: "emote", 0x0B: "text_emote", 0x11: "channel",
}
CHAT_MSG_CHANNEL = 0x11

# Bounded so a chatty channel can't grow this without limit.
CHAT_INBOX_MAXLEN = 50

# Once a packet's first byte has arrived, the rest must follow within this long.
# A timeout mid-packet would desync framing and RC4 state, so it ends the session.
MID_PACKET_TIMEOUT_S = 30.0
# Handler errors are logged at most once per opcode per this many seconds.
ERROR_LOG_INTERVAL_S = 30.0


def _read_len_string(data: bytes, off: int) -> tuple[str, int]:
    """A uint32-length-prefixed string (length includes the trailing null),
    as ChatPackets.cpp writes SenderName/ChatText — distinct from
    agent.packets.cstring's null-scan, which is for the plain cstrings used
    elsewhere in the same packet (e.g. channel name)."""
    length = struct.unpack_from('<I', data, off)[0]; off += 4
    s = data[off:off + length - 1].decode('utf-8', 'replace') if length > 0 else ""
    off += length
    return s, off


class _ErrorThrottle:
    """Rate-limits log lines per key, counting the ones it suppresses."""

    def __init__(self, interval: float, clock=time.monotonic):
        self.interval = interval
        self._clock = clock
        self._last = {}        # key -> time of last emitted line
        self._suppressed = {}  # key -> errors swallowed since then

    def check(self, key) -> tuple[bool, int]:
        """Returns (should_log, suppressed_since_last_log)."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, 0
        self._last[key] = now
        return True, self._suppressed.pop(key, 0)


class WoWSession:
    """Manages a single character's World of Warcraft session."""

    def __init__(self, host: str, port: int, account_name: str,
                 session_key: bytes, realm_id: int, verbose_packets: bool = False,
                 dump_packets_dir: str = ""):
        self.host = host
        self.port = port
        self.account_name = account_name
        self.session_key = session_key
        self.realm_id = realm_id
        self.verbose_packets = verbose_packets
        self.dump_packets_dir = dump_packets_dir
        self.sock = None
        self.crypt = None
        self._recv_thread = None
        self._running = False
        self._in_world = False
        self._lock = threading.Lock()
        self._error_throttle = _ErrorThrottle(ERROR_LOG_INTERVAL_S)
        self.dropped_packets = 0  # packets whose handler raised

        # Game state
        self.player_guid = 0
        self.player_name = ""
        self.player_position = None  # (map_id, x, y, z, orient)
        self.level = 0
        self.race = 0  # ChrRaces.dbc ID; set by callers (e.g. __main__.py) from enum_characters()
        self.xp = None
        self.next_level_xp = None
        self.coinage = None
        self.world_state = per.WorldState()  # nearby objects, players, etc.
        self.on_update_object = None  # callback(update_type, guid, fields)
        self.chat_inbox = collections.deque(maxlen=CHAT_INBOX_MAXLEN)
        self.pending_invite = None  # {"inviter_name": str} or None

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
        # Set before any update-object arrives so perception can tell our
        # own blocks from everyone else's.
        self.player_guid = guid
        self.world_state.set_my_guid(guid)
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
                self._in_world = True
                self._running = True
                self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
                self._recv_thread.start()
                return True
            if not self._dispatch_guarded(opcode, payload) and self.verbose_packets:
                log.info("pre-login: %#05x (%d B)", opcode, len(payload))

        raise TimeoutError("Login timed out")

    def recv_thread_alive(self) -> bool:
        return self._recv_thread is not None and self._recv_thread.is_alive()

    def logout(self):
        """Graceful logout. Safe to call at any stage, including after the
        recv thread has died."""
        if self.sock is None:
            return
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=5)
        if self._in_world:
            self._in_world = False
            try:
                self._send_packet(CMSG_LOGOUT_REQUEST)
                self.sock.settimeout(5)
                while True:
                    opcode, payload = self._recv_packet()
                    if opcode == SMSG_LOGOUT_COMPLETE:
                        break
            except Exception:
                pass
        self.sock.close()
        self.sock = None

    def send_chat(self, message: str, channel: str = "say", target: str | None = None):
        """Send a chat message. Channel: 'say', 'yell', 'whisper' (needs
        `target`), 'emote'. Prefer agent.actions.say/yell/whisper/emote
        directly in new code."""
        from . import actions
        actions.send_chat_message(self, message, channel, target=target)

    # ── Internal ──────────────────────────────────────────────

    def _handle_verify_world(self, payload):
        off = 0
        map_id = struct.unpack_from('<i', payload, off)[0]; off += 4
        px = struct.unpack_from('<f', payload, off)[0]; off += 4
        py = struct.unpack_from('<f', payload, off)[0]; off += 4
        pz = struct.unpack_from('<f', payload, off)[0]; off += 4
        orient = struct.unpack_from('<f', payload, off)[0]
        self.player_position = (map_id, px, py, pz, orient)
        self.world_state.set_my_map(map_id)

    def _handle_destroy_object(self, payload: bytes):
        # Object::DestroyForPlayer (Object.cpp): uint64 guid, uint8 onDeath.
        guid = pk.u64(payload, 0)
        self.world_state.remove_guids([guid])

    def _handle_update_object(self, opcode: int, payload: bytes):
        if opcode == SMSG_COMPRESSED_UPDATE_OBJECT:
            unc_size = pk.u32(payload, 0)
            data = zlib.decompress(payload[4:])
            if len(data) != unc_size:
                raise per.PerceptionParseError(
                    f"inflated to {len(data)} B, header said {unc_size} B")
        else:
            data = payload
        if self.dump_packets_dir:
            self._dump_packet(opcode, data)
        self._parse_update_object(data)

    def _dump_packet(self, opcode: int, data: bytes):
        # A dump failure (disk full, bad path) must not cost us the packet.
        try:
            os.makedirs(self.dump_packets_dir, exist_ok=True)
            path = os.path.join(self.dump_packets_dir, f"{time.time_ns()}_{opcode:#06x}.bin")
            with open(path, 'wb') as f:
                f.write(data)
        except OSError as e:
            if self._error_throttle.check('dump')[0]:
                log.warning("AGENT_DUMP_PACKETS: could not write dump: %s", e)

    def _parse_update_object(self, data: bytes):
        """Parse an (inflated) SMSG_UPDATE_OBJECT payload into world state.

        Full block framing + movement parsing lives in agent/update_object.py
        (pure function, no I/O); field mapping in agent/update_fields.py. This
        wires the result into WorldState (create/merge/remove) and, for
        blocks about our own player, mirrors position and a few stats onto
        the session directly for cheap access without going through
        world_state.

        Raises PerceptionParseError on truncated or malformed data, or lets
        UnhandledMovementFlags propagate (an unimplemented conditional path,
        e.g. spline movement) — either way the caller's per-packet safety net
        (_dispatch_guarded) drops just this packet and keeps the connection.
        Anything recorded from earlier packets is kept.
        """
        try:
            blocks = uo.parse_update_object(data)
        except (IndexError, struct.error, ValueError) as e:
            raise per.PerceptionParseError(
                f"malformed update-object payload ({len(data)} B): {e}") from e
        for block in blocks:
            if block.update_type in (UPDATETYPE_OUT_OF_RANGE_OBJECTS, UPDATETYPE_NEAR_OBJECTS):
                self.world_state.remove_guids(block.guids)
                continue
            self.world_state.update_object(block)
            if block.guid == self.player_guid:
                self._sync_self_from_block(block)

    def _sync_self_from_block(self, block):
        """Mirror our own object's position/stats from world_state onto the
        session for cheap direct access (session.player_position, .level, ...)."""
        movement = block.movement or {}
        if "x" in movement:
            map_id = self.player_position[0] if self.player_position else self.world_state.my_map
            self.player_position = (map_id, movement["x"], movement["y"], movement["z"], movement.get("o", 0.0))
        me = self.world_state.get_my_object()
        if me is not None:
            if me.level is not None:
                self.level = me.level
            xp = me.raw_fields.get(uo_fields.PLAYER_XP)
            if xp is not None:
                self.xp = xp
            next_xp = me.raw_fields.get(uo_fields.PLAYER_NEXT_LEVEL_XP)
            if next_xp is not None:
                self.next_level_xp = next_xp
            coinage = me.raw_fields.get(uo_fields.PLAYER_FIELD_COINAGE)
            if coinage is not None:
                self.coinage = coinage

    def _handle_messagechat(self, opcode: int, payload: bytes):
        """SMSG_MESSAGECHAT / SMSG_GM_MESSAGECHAT (WorldPackets::Chat::Chat::Write,
        ChatPackets.cpp) — the "default" branch (covers say/yell/whisper/
        party/guild/officer/emote/channel; the MONSTER_*/BG_SYSTEM_*/
        ACHIEVEMENT branches use a different layout and aren't parsed here —
        a parse error just drops that one message via _dispatch_guarded).

        Layout: uint8 type, int32 language, uint64 sender_guid, uint32 flags,
        [only on SMSG_GM_MESSAGECHAT: uint32 name_len + name], [only for
        CHAT_MSG_CHANNEL: cstring channel], uint64 target_guid,
        uint32 text_len + text, uint8 chat_tag.
        """
        off = 0
        slash_cmd = payload[off]; off += 1
        off += 4  # language (int32) — not needed by callers yet
        sender_guid = pk.u64(payload, off); off += 8
        off += 4  # flags (uint32), always 0 in 3.3.5
        sender_name = ""
        if opcode == SMSG_GM_MESSAGECHAT:
            sender_name, off = _read_len_string(payload, off)
        channel = None
        if slash_cmd == CHAT_MSG_CHANNEL:
            channel, off = pk.cstring(payload, off)
        off += 8  # target_guid — not needed by callers yet
        text, off = _read_len_string(payload, off)

        self.chat_inbox.append({
            "kind": CHAT_KIND_NAMES.get(slash_cmd, f"type_{slash_cmd}"),
            "sender_guid": sender_guid,
            "sender_name": sender_name,
            "channel": channel,
            "text": text,
        })

    def _handle_group_invite(self, payload: bytes):
        # PartyInvite::Write (PartyPackets.cpp): uint8 can_accept, cstring
        # inviter_name, uint32 proposed_roles, ... (LFG fields, ignored).
        off = 1  # skip can_accept
        inviter_name, off = pk.cstring(payload, off)
        self.pending_invite = {"inviter_name": inviter_name}
        log.info("group invite from %s", inviter_name)

    def _send_sync(self, payload):
        counter = struct.unpack_from('<I', payload, 0)[0]
        self._send_packet(CMSG_TIME_SYNC_RESP, struct.pack('<II', counter, 0))

    def _dispatch(self, opcode: int, payload: bytes) -> bool:
        """Handle one packet. Returns False if the opcode isn't handled."""
        if opcode == SMSG_TIME_SYNC_REQ:
            self._send_sync(payload)
        elif opcode in (SMSG_COMPRESSED_UPDATE_OBJECT, SMSG_UPDATE_OBJECT):
            self._handle_update_object(opcode, payload)
        elif opcode in (SMSG_MESSAGECHAT, SMSG_GM_MESSAGECHAT):
            self._handle_messagechat(opcode, payload)
        elif opcode == SMSG_GROUP_INVITE:
            self._handle_group_invite(payload)
        elif opcode == SMSG_DESTROY_OBJECT:
            self._handle_destroy_object(payload)
        elif opcode in (SMSG_PONG, SMSG_MONSTER_MOVE, SMSG_STANDSTATE_UPDATE):
            # SMSG_MONSTER_MOVE (NPC destinations) and MSG_MOVE_* heartbeats
            # (other players' positions) aren't parsed — see docs/ROADMAP.md.
            # Nearby objects still get position updates from their own
            # periodic update-object blocks, just not every movement tick.
            pass
        elif opcode == SMSG_LOGOUT_COMPLETE:
            self._in_world = False
            self._running = False
        else:
            return False
        return True

    def _dispatch_guarded(self, opcode: int, payload: bytes) -> bool:
        """_dispatch, but a handler error drops only this packet, never the
        connection. Errors are logged at most once per opcode per interval."""
        try:
            return self._dispatch(opcode, payload)
        except Exception:
            self.dropped_packets += 1
            should_log, suppressed = self._error_throttle.check(opcode)
            if should_log:
                log.warning("dropped %#05x packet (%d B) after handler error "
                            "(%d more on this opcode suppressed since last report)",
                            opcode, len(payload), suppressed, exc_info=True)
            return True

    def _recv_loop(self):
        """Background thread: read packets and dispatch."""
        try:
            self._recv_until_stopped()
        except Exception:
            log.exception("recv thread crashed")
        finally:
            if self._running:
                log.warning("recv thread exited while the session was still running")
                self._running = False

    def _recv_until_stopped(self):
        last_keepalive = time.monotonic()
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
            except ConnectionError as e:
                log.warning("world connection lost: %s", e)
                return
            self._dispatch_guarded(opcode, payload)

    def _send_packet(self, opcode: int, payload: bytes = b''):
        hdr = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        if self.crypt:
            hdr = self.crypt.encrypt_send(hdr)
        with self._lock:
            self.sock.sendall(hdr + payload)

    def _recv_packet(self) -> tuple[int, bytes]:
        # Only this first read may time out: nothing has been consumed yet.
        first = self._rr(1)
        prev_timeout = self.sock.gettimeout()
        if prev_timeout is not None and prev_timeout < MID_PACKET_TIMEOUT_S:
            self.sock.settimeout(MID_PACKET_TIMEOUT_S)
        try:
            hdr = first + self._rr(3)
            if self.crypt:
                hdr = self.crypt.decrypt_recv(hdr)
            if hdr[0] & 0x80:  # large packet: one more size byte before the opcode
                extra = self._rr(1)
                if self.crypt:
                    extra = self.crypt.decrypt_recv(extra)
                hdr += extra
            size, opcode = pk.parse_server_header(hdr)
            plen = size - 2
            payload = self._rr(plen) if plen > 0 else b''
        except (socket.timeout, TimeoutError) as e:
            raise ConnectionError("timed out mid-packet, stream out of sync") from e
        finally:
            self.sock.settimeout(prev_timeout)
        return opcode, payload

    def _rr(self, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("disconnected")
            buf += c
        return buf