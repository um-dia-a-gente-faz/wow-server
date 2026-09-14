#!/usr/bin/env python3
"""Game actions — say, target, attack, move for the agent."""

import struct

CMSG_MESSAGECHAT  = 0x095   # chat say/yell/whisper
CMSG_SET_SELECTION = 0x13D  # target a GUID
CMSG_STAND_STATE_CHANGE = 0x101
CMSG_ATTACKSWING   = 0x141
CMSG_ATTACKSTOP    = 0x142


def send_chat_message(session, message: str, channel: str = "say"):
    """Send a chat message. Channel: 'say' (0), 'yell' (1), 'whisper' (2)."""
    channel_map = {'say': 0, 'yell': 1, 'party': 8, 'guild': 17}
    msg_type = channel_map.get(channel, 0)
    lang = 1  # LANG_ORCISH? No, use COMMON/LANG_UNIVERSAL

    # CMSG_MESSAGECHAT: uint32 type, uint32 lang, [optional: whisper target cstring], message cstring
    payload = struct.pack('<II', msg_type, lang)
    if channel == 'whisper':
        payload += b'target\x00'  # placeholder — real whisper needs recipient name
    payload += message.encode('ascii', errors='replace') + b'\x00'
    session._send_packet(CMSG_MESSAGECHAT, payload)


def send_target(session, guid: int):
    """Target a unit or object by GUID."""
    session._send_packet(CMSG_SET_SELECTION, struct.pack('<Q', guid))


def send_attack(session, guid: int):
    """Start auto-attack on target."""
    send_target(session, guid)
    session._send_packet(CMSG_ATTACKSWING)