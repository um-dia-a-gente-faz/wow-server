#!/usr/bin/env python3
"""WoW session manager — login, keepalive, packet dispatch.
Wraps the low-level world protocol in a background recv loop.

Layout: transport.py (framing/crypto/socket), state.py (game state),
router.py (opcode -> handler table), handlers/ (one module per domain,
registering on the router at import). This module is the login flow and
the recv loop that ties them together."""

import logging
import socket
import struct
import threading
import time

from . import channels as ch_mod
from . import crypt as cr
from . import packets as pk
from .router import Context, ROUTER
from .metrics import swallowed
from .state import GameState
from .transport import Transport, _ErrorThrottle  # noqa: F401  (_ErrorThrottle re-exported for tests)

# Importing a handler module registers its opcodes (and per-tick query
# senders) on ROUTER. Tick order = import order: names, npc text, items, quests.
from .handlers import world as _world
from .handlers import chat, death, group, mail, npc, spells, trade  # noqa: F401
from .handlers import loot, quests  # noqa: F401

log = logging.getLogger("agent.session")

# Opcodes (TrinityCore 3.3.5 Opcodes.h)
# Opcodes (TrinityCore 3.3.5 Opcodes.h)
from .opcodes import (
    CMSG_CHAR_CREATE,
    CMSG_CHAR_ENUM,
    SMSG_CHAR_CREATE,
    SMSG_CHAR_ENUM,
    CMSG_PLAYER_LOGIN,
    CMSG_LOGOUT_REQUEST,
    SMSG_LOGOUT_COMPLETE,
    SMSG_TUTORIAL_FLAGS,
    CMSG_PING,
    SMSG_PONG,
    SMSG_AUTH_CHALLENGE,
    CMSG_AUTH_SESSION,
    SMSG_AUTH_RESPONSE,
    SMSG_LOGIN_VERIFY_WORLD,
    CMSG_SET_ACTIVE_MOVER,
    SMSG_STANDSTATE_UPDATE,
    SMSG_TIME_SYNC_REQ,
    CMSG_TIME_SYNC_RESP,
    CMSG_KEEP_ALIVE,
)
CHAR_CREATE_SUCCESS     = 47  # SharedDefines.h ResponseCodes (0x2F)
CHAR_CREATE_MAX_SKIPPED_PACKETS = 32
CHAR_CREATE_NAME_IN_USE = 50  # 0x32


# Liveness of the world socket (#404), checked against TrinityCore 3.3.5 WorldSocket.cpp /
# WorldSession.cpp / worldserver.conf.dist. CMSG_KEEP_ALIVE gets no reply but resets the
# server's idle timer (SocketTimeOutTimeActive, 60 s). CMSG_PING gets SMSG_PONG, but pings
# less than 27 s apart count as over-speed and kick past MaxOverspeedPings (2), so the ping
# period stays above 27 s (the real client uses 30 s). The deadline is the longest silence
# (no packet of any kind) tolerated before the socket is declared dead and the reconnect
# supervisor takes over; it must exceed PING_INTERVAL_S plus a round trip.
KEEPALIVE_INTERVAL_S = 15.0
PING_INTERVAL_S = 30.0
DEAD_SOCKET_TIMEOUT_S = 45.0


class WoWSession(Transport, GameState):
    """Manages a single character's World of Warcraft session."""

    def __init__(self, host: str, port: int, account_name: str,
                 session_key: bytes, realm_id: int, verbose_packets: bool = False,
                 dump_packets_dir: str = ""):
        Transport.__init__(self, dump_packets_dir)
        GameState.__init__(self)
        self.host = host
        self.port = port
        self.account_name = account_name
        self.session_key = session_key
        self.realm_id = realm_id
        self.verbose_packets = verbose_packets
        self.ctx = Context(self, self)
        self._recv_thread = None

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
        import os
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
                handle_time_sync(self.ctx, payload)
            elif opcode == SMSG_TUTORIAL_FLAGS:
                break

        return True

    def create_character(self, name: str, race: int, class_: int, gender: int = 0,
                          skin: int = 0, face: int = 0, hair_style: int = 0,
                          hair_color: int = 0, facial_hair: int = 0) -> int:
        """Create a fresh level-1 character (CMSG_CHAR_CREATE; UM-55).

        Payload layout verified against TrinityCore 3.3.5a
        WorldPackets::Character::CreateCharacter::Read() (CharacterPackets.cpp):
        null-terminated name, then one uint8 each for race, class, gender
        (Sex), skin, face, hair style, hair color, facial hair, outfit id
        (always 0, deprecated). Response is SMSG_CHAR_CREATE with a single
        uint8 result code (SharedDefines.h ResponseCodes); raises on
        anything but CHAR_CREATE_SUCCESS (47).
        """
        payload = (
            name.encode() + b'\x00'
            + bytes([race, class_, gender, skin, face, hair_style, hair_color, facial_hair])
            + b'\x00'  # OutfitId, deprecated
        )
        self._send_packet(CMSG_CHAR_CREATE, payload)
        # Found live 2026-10-03: the server builds the new Player on this session
        # before it answers, and that sends SMSG_POWER_UPDATE (0x480) first. Skip
        # whatever comes before the answer instead of failing on it.
        for _ in range(CHAR_CREATE_MAX_SKIPPED_PACKETS + 1):
            opcode, resp = self._recv_packet()
            if opcode == SMSG_CHAR_CREATE:
                break
        else:
            raise RuntimeError(f"char create failed for {name!r}: no SMSG_CHAR_CREATE "
                               f"in {CHAR_CREATE_MAX_SKIPPED_PACKETS + 1} packets")
        code = resp[0]
        if code != CHAR_CREATE_SUCCESS:
            raise RuntimeError(f"char create failed for {name!r}: response code {code}")
        return code

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
            _char_flags = struct.unpack_from('<I', payload, off)[0]; off += 4
            # Everything after the flags, exactly as TrinityCore 3.3.5's
            # Player::BuildEnumData() writes it (Player.cpp, branch 3.3.5):
            # a uint32 customizeFlags that is ALWAYS present (the old
            # `if char_flags & 0x08` guess skipped it for most characters), a
            # uint8 firstLogin, the pet's three uint32s, then one record per
            # INVENTORY_SLOT_BAG_END (=23) slot: uint32 displayId, uint8
            # inventoryType, uint32 enchantVisual = 9 bytes each. That is
            # 4 + 1 + 12 + 207 = 224 bytes. The old `4 + 12 + 23*4` (108)
            # started every character after the first 47 bytes early, so on an
            # account holding 2+ characters the agent could not find the one it
            # was told to play and logged into the wrong one (gh-199).
            off += 4       # customizeFlags
            off += 1       # firstLogin
            off += 12      # pet displayId, level, family
            off += 23 * 9  # equipment: displayId(u32), inventoryType(u8), enchantVisual(u32)
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
        self.world_state.names.load()  # UM-35: reuse the on-disk creature/gameobject cache, if any
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
                _world.handle_verify_world(self.ctx, payload)
                # A real client sends this right after login (CMSG_SET_ACTIVE_MOVER,
                # 0x26A) to claim itself as the unit it controls. Without it,
                # WorldSession::ValidateAndGetUnitBeingMoved (MovementHandler.cpp)
                # finds no GameClient::_activelyMovedUnit and silently drops every
                # MSG_MOVE_* we send (face, and later movement in UM-38) — found
                # while live-verifying UM-36's face action.
                self._send_packet(CMSG_SET_ACTIVE_MOVER, struct.pack('<Q', guid))
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
        self._logout_requested = True  # a SMSG_LOGOUT_COMPLETE after this is expected, not a drop (UM-43)
        if self.sock is None:
            return
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=5)
        # #406: after an unexpected disconnect (drop, desync, dead socket, server-forced
        # logout) nobody is there to answer a request, so just close.
        if self._in_world and not self.unexpected_disconnect:
            self._in_world = False
            try:
                self._send_packet(CMSG_LOGOUT_REQUEST)
                self.sock.settimeout(5)
                while True:
                    opcode, payload = self._recv_packet()
                    if opcode == SMSG_LOGOUT_COMPLETE:
                        break
            except Exception:
                swallowed("session.logout_wait", log, level=logging.DEBUG)  # a dead socket is expected here
        self.sock.close()
        self.sock = None

    def send_chat(self, message: str, channel: str = "say", target: str | None = None):
        """Send a chat message. Channel: 'say', 'yell', 'whisper' (needs
        `target`), 'emote'. Prefer agent.actions.say/yell/whisper/emote
        directly in new code."""
        from . import actions
        actions.send_chat_message(self, message, channel, target=target)

    def join_channels(self, names, timeout: float = 5.0) -> dict:
        """UM-93: join chat channels (CMSG_JOIN_CHANNEL, agent.channels) and
        wait for each one's you_joined notice or error. The server doesn't
        auto-join General at login (a real client asks for it itself, see
        agent/channels.py), so callers do this right after
        login_character(). Returns {requested name: full joined name, or
        None if it didn't join in time / was refused}. A system channel
        the zone doesn't have (e.g. Trade outside a city) is dropped
        silently by the server, so it just times out."""
        results = {}
        for name in names:
            sent_at = time.monotonic()
            try:
                payload = ch_mod.build_join_channel(name)
            except ValueError as e:
                log.warning("not joining channel %r: %s", name, e)
                results[name] = None
                continue
            self._send_packet(ch_mod.CMSG_JOIN_CHANNEL, payload)
            cid = ch_mod.system_channel_id(name)
            deadline = sent_at + timeout
            joined = None
            while joined is None and time.monotonic() < deadline:
                for e in list(self.events):
                    if e.get("t", 0) < sent_at:
                        continue
                    if e.get("kind") == "channel_joined" and (
                            (cid and e.get("channel_id") == cid)
                            or e.get("channel", "").lower() == name.lower()):
                        joined = e["channel"]
                        break
                    if e.get("kind") == "channel_error" and e.get("channel", "").lower().startswith(name.lower()):
                        joined = False
                        break
                else:
                    time.sleep(0.1)
            results[name] = joined or None
            if joined:
                log.info("joined channel %r", joined)
            else:
                log.warning("could not join channel %r", name)
        return results

    def _dispatch(self, opcode: int, payload: bytes) -> bool:
        """Handle one packet. Returns False if the opcode isn't handled."""
        return ROUTER.dispatch(self.ctx, opcode, payload)

    def _dispatch_guarded(self, opcode: int, payload: bytes) -> bool:
        """_dispatch, but a handler error drops only this packet, never the
        connection. Errors are logged at most once per opcode per interval."""
        try:
            return self._dispatch(opcode, payload)
        except Exception:
            self.dropped_packets += 1
            swallowed("session.dispatch", log, f"dropped {opcode:#05x} packet, {len(payload)} B",
                      key=opcode, throttle=self._error_throttle, level=logging.WARNING)
            return True

    def _recv_loop(self):
        """Background thread: read packets and dispatch."""
        try:
            self._recv_until_stopped()
        except Exception:
            swallowed("session.recv_thread", log)
        finally:
            if self._running:
                log.warning("recv thread exited while the session was still running")
                self._running = False
                # Not a requested logout() (that sets _running=False itself
                # before joining this thread) — the reconnect supervisor
                # (agent/__main__.py, UM-43) treats this as a drop worth
                # retrying rather than the agent process exiting.
                self.unexpected_disconnect = True

    def _recv_until_stopped(self):
        now = time.monotonic()
        last_rx = last_keepalive = last_ping = now
        ping_id = 0
        while self._running:
            self.sock.settimeout(0.5)
            try:
                opcode, payload = self._recv_packet()
            except (socket.timeout, TimeoutError):
                now = time.monotonic()
                if now - last_rx > DEAD_SOCKET_TIMEOUT_S:
                    # #404: connection open, nothing arrives, not even SMSG_PONG.
                    log.warning("world socket silent for %.0f s, treating it as dead", now - last_rx)
                    return
                if now - last_keepalive > KEEPALIVE_INTERVAL_S:
                    self._send_packet(CMSG_KEEP_ALIVE)
                    last_keepalive = now
                if now - last_ping > PING_INTERVAL_S:
                    ping_id += 1
                    self._send_packet(CMSG_PING, struct.pack('<II', ping_id, 0))
                    last_ping = now
                ROUTER.tick(self.ctx)
                continue
            except ConnectionError as e:
                log.warning("world connection lost: %s", e)
                return
            last_rx = time.monotonic()
            self._dispatch_guarded(opcode, payload)
            ROUTER.tick(self.ctx)


# ── Connection-lifecycle packets (the only handlers that live here) ─────────

def handle_time_sync(ctx, payload):
    counter = struct.unpack_from('<I', payload, 0)[0]
    ctx.send(CMSG_TIME_SYNC_RESP, struct.pack('<II', counter, 0))


def handle_logout_complete(ctx, payload):
    st = ctx.state
    if not st._logout_requested:
        # The server ended our session without us asking (kicked,
        # duplicate login elsewhere, forced disconnect) — the
        # reconnect supervisor (agent/__main__.py, UM-43) treats
        # this the same as a dropped socket.
        st.unexpected_disconnect = True
    st._in_world = False
    st._running = False


ROUTER.register_all({
    SMSG_TIME_SYNC_REQ: handle_time_sync,
    SMSG_PONG: lambda ctx, payload: None,
    SMSG_STANDSTATE_UPDATE: lambda ctx, payload: None,
    SMSG_LOGOUT_COMPLETE: handle_logout_complete,
})
