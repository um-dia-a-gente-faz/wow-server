"""Shared packet builders and fakes for the agent tests and tools/world-mock.

Byte layouts here are the ones the agent's parsers already consume; where a
layout is not self-evident the docstring cites the TrinityCore 3.3.5 writer.
Before #253 each test file rebuilt these by hand (test_session,
test_update_object_parser, test_loot, test_chat_relay, test_quests); import
from here instead of adding another copy.
"""

import os
import socket
import struct
import tempfile
import uuid
import zlib

from agent import packets as pk
from agent import session as se
from agent import state
from agent import update_object as uo

# update_object.UPDATETYPE_OUT_OF_RANGE_OBJECTS lives in handlers.world.
UPDATETYPE_OUT_OF_RANGE_OBJECTS = 4


def tc_server_header(size: int, opcode: int) -> bytes:
    """Port of TrinityCore 3.3.5 ServerPktHeader's constructor (size includes
    the 2 opcode bytes)."""
    out = bytearray()
    if size > 0x7FFF:
        out.append(0x80 | (0xFF & (size >> 16)))
    out.append(0xFF & (size >> 8))
    out.append(0xFF & size)
    out.append(0xFF & opcode)
    out.append(0xFF & (opcode >> 8))
    return bytes(out)


def server_packet(opcode: int, payload: bytes) -> bytes:
    """A whole unencrypted server->client packet: header + payload."""
    return tc_server_header(len(payload) + 2, opcode) + payload


def cstr(s: str) -> bytes:
    return s.encode('utf-8') + b'\x00'


class FakeSocket:
    """Serves a fixed byte stream in chunks; EOF (b'') when exhausted."""

    def __init__(self, data: bytes, chunk: int = 7, timeout_at: int | None = None):
        self.data = data
        self.pos = 0
        self.chunk = chunk
        self.timeout_at = timeout_at  # stream offset at which recv times out
        self.timeout = None
        self.sent = b''

    def recv(self, n):
        if self.timeout_at is not None and self.pos >= self.timeout_at:
            raise socket.timeout("timed out")
        end = min(self.pos + n, self.pos + self.chunk, len(self.data))
        if self.timeout_at is not None:
            end = min(end, self.timeout_at)
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def settimeout(self, t):
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def sendall(self, b):
        self.sent += b

    def close(self):
        pass


def make_session(stream: bytes = b'', **kw) -> se.WoWSession:
    """A WoWSession on a FakeSocket. The name cache is pointed at a temp file:
    a "found" creature/gameobject query response saves it, and a test must
    never touch the real ~/.cache/wow-agent/names.json."""
    sess = se.WoWSession('127.0.0.1', 8085, 'TEST', b'\x00' * 40, 1, **kw)
    sess.sock = FakeSocket(stream)
    sess.world_state.names.cache_path = os.path.join(
        tempfile.gettempdir(), f"wow-agent-test-names-{uuid.uuid4().hex}.json")
    return sess


class FakeSession(state.GameState):
    """The session as actions and reflexes see it (agent.ports), without a socket.

    It is the real GameState, so `record_event` and every attribute a port names
    are the production ones; only sending is faked. Keyword arguments override
    attributes, e.g. `FakeSession(player_guid=ME, events=[])` for a plain list of
    events. `send_packet` goes through `_send_packet`, as on Transport, so a test
    that replaces `_send_packet` sees both names; movement.py and death.py still
    use the private one."""

    def __init__(self, **attrs):
        super().__init__()
        self.sent: list[tuple[int, bytes]] = []
        self._sent = self.sent  # the name the older tests read
        for name, value in attrs.items():
            setattr(self, name, value)

    def send_packet(self, opcode: int, payload: bytes = b'') -> None:
        self._send_packet(opcode, payload)

    def _send_packet(self, opcode: int, payload: bytes = b'') -> None:
        self.sent.append((opcode, payload))


# ── SMSG_UPDATE_OBJECT ─────────────────────────────────────────────────────

def update_object(*blocks: bytes) -> bytes:
    return struct.pack('<I', len(blocks)) + b''.join(blocks)


def stationary_movement(x=1.0, y=2.0, z=3.0, o=0.5) -> bytes:
    """A minimal, valid movement sub-block: UPDATEFLAG_STATIONARY_POSITION
    only (uint16 flags + 4 floats) — the smallest real conditional path."""
    return struct.pack('<H', uo.UPDATEFLAG_STATIONARY_POSITION) + struct.pack('<4f', x, y, z, o)


def no_values() -> bytes:
    """A VALUES_UPDATE with zero mask words set (nothing changed)."""
    return b'\x00'


def values_body(field_values: dict) -> bytes:
    """uint8 mask_block_count, mask words, one uint32 per set bit ascending
    (all mask words first, then all values: see the trinity-protocol skill)."""
    if not field_values:
        return b'\x00'
    max_bit = max(field_values)
    block_count = max_bit // 32 + 1
    words = [0] * block_count
    for idx in field_values:
        words[idx // 32] |= 1 << (idx % 32)
    out = bytes([block_count]) + b''.join(struct.pack('<I', w) for w in words)
    for idx in sorted(field_values):
        out += struct.pack('<I', field_values[idx])
    return out


def object_block(update_type: int, guid: int, object_type: int = uo.TYPEID_UNIT,
                 movement: bytes = None, values: bytes = None) -> bytes:
    """A CREATE_OBJECT[2] block body: packed guid, object type, movement, values."""
    if movement is None:
        movement = stationary_movement()
    if values is None:
        values = no_values()
    return bytes([update_type]) + pk.pack_packed_guid(guid) + bytes([object_type]) + movement + values


def values_block(guid: int, values: bytes = None) -> bytes:
    """A VALUES block body: packed guid, values."""
    if values is None:
        values = no_values()
    return bytes([uo.UPDATETYPE_VALUES]) + pk.pack_packed_guid(guid) + values


def out_of_range_block(*guids: int) -> bytes:
    return (bytes([UPDATETYPE_OUT_OF_RANGE_OBJECTS]) + struct.pack('<I', len(guids))
            + b''.join(pk.pack_packed_guid(g) for g in guids))


def compressed(payload: bytes) -> bytes:
    """SMSG_COMPRESSED_UPDATE_OBJECT body: uint32 inflated size + zlib stream."""
    return struct.pack('<I', len(payload)) + zlib.compress(payload)


# ── Quest packets ──────────────────────────────────────────────────────────

def reward_list(items) -> bytes:
    """uint32 count + (item id, count, display id) triples, as in
    QuestGiverOfferRewardMessage::Write (QuestPackets.cpp)."""
    return struct.pack('<I', len(items)) + b''.join(struct.pack('<III', e, c, d) for e, c, d in items)


def offer_reward_payload(npc_guid: int, quest_id: int, title: str, text: str,
                         choices=(), rewards=(), money: int = 0, xp: int = 0) -> bytes:
    """SMSG_QUESTGIVER_OFFER_REWARD, field order per
    WorldPackets::Quest::QuestGiverOfferRewardMessage::Write (QuestPackets.cpp,
    TrinityCore 3.3.5): guid, quest id, title, reward text, auto-launched,
    flags, suggested players, emotes, choice items, reward items, money, xp,
    honor, kill honor, unused, display spell, spell, title id, talents, arena,
    faction flags, then 5 faction ids + 5 values + 5 overrides."""
    return (
        struct.pack('<QI', npc_guid, quest_id) + cstr(title) + cstr(text)
        + bytes([1]) + struct.pack('<II', 0, 0)
        + struct.pack('<I', 0)  # no emotes
        + reward_list(choices) + reward_list(rewards)
        + struct.pack('<III', money, xp, 0) + struct.pack('<f', 0.0)
        + struct.pack('<IIiIIII', 0, 0, 0, 0, 0, 0, 0)
        + struct.pack('<15i', *([0] * 15))
    )


def quest_complete_payload(quest_id: int, xp: int = 0, money: int = 0) -> bytes:
    """SMSG_QUESTGIVER_QUEST_COMPLETE: Player::SendQuestReward (Player.cpp,
    TrinityCore 3.3.5): quest id, xp, money, honor, bonus talents, arena."""
    return struct.pack('<6I', quest_id, xp, money, 0, 0, 0)
