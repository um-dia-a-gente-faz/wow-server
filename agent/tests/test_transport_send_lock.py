"""Transport._send_packet advances the header cipher under the send lock (#660)."""

import socket
import struct
import threading
import unittest

from agent.crypt import WorldCrypt
from agent.transport import Transport

JOIN_S = 60.0


class _RecordingCrypt:
    def __init__(self, transport):
        self.transport = transport
        self.locked = []

    def encrypt_send(self, data):
        self.locked.append(self.transport._lock.locked())
        return data


class SendLockTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.t = Transport()
        self.t.sock = self.a

    def test_header_is_encrypted_while_the_lock_is_held(self):
        self.t.crypt = _RecordingCrypt(self.t)
        self.t._send_packet(0x1234, b"abc")
        self.assertEqual(self.t.crypt.locked, [True])

    def test_single_sender_bytes_are_the_headers_encrypted_in_order(self):
        key = bytes(range(40))
        self.t.crypt = WorldCrypt(key)
        ref = WorldCrypt(key)
        want = b""
        for i in range(20):
            payload = bytes([i]) * i
            self.t._send_packet(0x100 + i, payload)
            hdr = struct.pack(">H", len(payload) + 4) + struct.pack("<I", 0x100 + i)
            want += ref.encrypt_send(hdr) + payload
        self.a.shutdown(socket.SHUT_WR)
        got = b""
        while chunk := self.b.recv(65536):
            got += chunk
        self.assertEqual(got, want)

    def test_eight_threads_keep_the_cipher_in_wire_order(self):
        threads_n, per_thread = 8, 500
        key = bytes(range(40))
        self.t.crypt = WorldCrypt(key)
        peer = WorldCrypt(key)
        sent = {(i * 7 + 1 + 4, 0x1000 + i) for i in range(threads_n)}  # (size, opcode)
        seen, errors = [], []

        # A second WorldCrypt's send RC4 is the same keystream; advanced in arrival
        # order it is what the server's decrypt does.
        def reader():
            try:
                for _ in range(threads_n * per_thread):
                    hdr = b""
                    while len(hdr) < 6:
                        c = self.b.recv(6 - len(hdr))
                        if not c:
                            raise ConnectionError("eof")
                        hdr += c
                    plain = peer.encrypt_send(hdr)
                    size, opcode = struct.unpack(">H", plain[:2])[0], struct.unpack("<I", plain[2:])[0]
                    if (size, opcode) not in sent:
                        raise AssertionError(f"garbage header {size:#x} {opcode:#x}")
                    left = size - 4
                    while left:
                        c = self.b.recv(left)
                        if not c:
                            raise ConnectionError("eof")
                        left -= len(c)
                    seen.append((size, opcode))
            except (AssertionError, OSError) as e:
                errors.append(e)
                self.b.close()

        def sender(i):
            payload = b"x" * (i * 7 + 1)
            for _ in range(per_thread):
                self.t._send_packet(0x1000 + i, payload)

        rt = threading.Thread(target=reader, daemon=True)
        ts = [threading.Thread(target=sender, args=(i,), daemon=True) for i in range(threads_n)]
        rt.start()
        for t in ts:
            t.start()
        for t in ts + [rt]:
            t.join(JOIN_S)
            self.assertFalse(t.is_alive(), f"thread still running after {JOIN_S}s")
        self.assertEqual(errors, [])
        self.assertEqual(len(seen), threads_n * per_thread)
