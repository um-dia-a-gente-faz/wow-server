#!/usr/bin/env python3
"""RC4 + WorldPacketCrypt for WotLK 3.3.5a (TrinityCore-style)."""

import hashlib
import hmac


class RC4:
    def __init__(self, key: bytes):
        self._init(key)

    def _init(self, key: bytes):
        self._state = bytearray(range(256))
        j = 0
        for i in range(256):
            j = (j + self._state[i] + key[i % len(key)]) % 256
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
    """WotLK header crypt: HMAC-SHA1(magic, session_key) -> RC4 with 1024-byte drop."""

    SERVER_ENCRYPTION_KEY = bytes([
        0xCC, 0x98, 0xAE, 0x04, 0xE8, 0x97, 0xEA, 0xCA,
        0x12, 0xDD, 0xC0, 0x93, 0x42, 0x91, 0x53, 0x57,
    ])
    SERVER_DECRYPTION_KEY = bytes([
        0xC2, 0xB3, 0x72, 0x3C, 0xC6, 0xAE, 0xD9, 0xB5,
        0x34, 0x3C, 0x53, 0xEE, 0x2F, 0x43, 0x67, 0xCE,
    ])

    def __init__(self, session_key: bytes):
        assert len(session_key) == 40
        recv_key = hmac.new(self.SERVER_ENCRYPTION_KEY, session_key, hashlib.sha1).digest()
        send_key = hmac.new(self.SERVER_DECRYPTION_KEY, session_key, hashlib.sha1).digest()
        self._send = RC4(send_key)
        self._recv = RC4(recv_key)
        self._send.drop(1024)
        self._recv.drop(1024)

    def decrypt_recv(self, data: bytes) -> bytes:
        return self._recv.crypt(data)

    def encrypt_send(self, data: bytes) -> bytes:
        return self._send.crypt(data)