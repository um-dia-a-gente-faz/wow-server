"""Auth handshake against a fake auth socket (#431): a challenge with security flags
(PIN 0x01, matrix 0x02, authenticator token 0x04) is refused before any proof byte or
SRP6 work, and a flags=0 challenge still reaches the proof step."""

import struct
import unittest
from unittest import mock

from agent import supervisor as main_mod
from agent import auth
from agent.auth import AuthRejected, AuthSecurityUnsupported
from agent.wire import PacketTruncated

USERNAME = "playerx"
PASSWORD = "hunter2"


def challenge(flags):
    """A 119-byte success challenge (cmd, error, result, B, g, N, s, crc, flags) followed by
    the extra bytes TrinityCore appends for each set flag: PIN 20, matrix 12 (4 x uint8 +
    uint64), token 1 (AuthSession.cpp LogonChallengeCallback)."""
    body = (bytes([0x00, 0x00, 0x00]) + bytes(32) + bytes([1, 7]) + bytes([32]) + bytes(32)
            + bytes(32) + bytes(16) + bytes([flags]))
    extra = 20 * (flags & 0x01) + 12 * ((flags >> 1) & 0x01) + 1 * ((flags >> 2) & 0x01)
    return body + bytes(extra)


PROOF_REJECT = bytes([0x01, 0x04, 0x00, 0x00])  # wrong password, 4 bytes


class FakeSocket:
    def __init__(self, *replies):
        self.inbox = b"".join(replies)
        self.sent = []

    def sendall(self, data):
        self.sent.append(bytes(data))

    def recv(self, n):
        chunk, self.inbox = self.inbox[:n], self.inbox[n:]
        return chunk


def no_srp6(*_args):
    raise AssertionError("compute_srp6 ran before the security flags were checked")


class SecurityFlagsTest(unittest.TestCase):
    def test_challenge_helper_matrix_flag_adds_12_extra_bytes(self):
        self.assertEqual(len(challenge(0x00)), 119)
        self.assertEqual(len(challenge(0x02)), 119 + 12)
        self.assertEqual(len(challenge(0x01)), 119 + 20)
        self.assertEqual(len(challenge(0x04)), 119 + 1)

    def test_flags_are_refused_before_any_proof_bytes_or_srp6(self):
        for flags in (0x01, 0x02, 0x04, 0x05, 0x07):
            with self.subTest(flags=flags):
                sock = FakeSocket(challenge(flags))
                with mock.patch.object(auth, "compute_srp6", side_effect=no_srp6):
                    with self.assertRaises(AuthSecurityUnsupported):
                        auth._handshake(sock, USERNAME, PASSWORD)
                # only the challenge request went out; no LOGON_PROOF (0x01) packet
                self.assertEqual([p[0] for p in sock.sent], [0x00])

    def test_error_is_an_auth_rejected_with_code_4_and_names_the_flags(self):
        sock = FakeSocket(challenge(0x05))
        with mock.patch.object(auth, "compute_srp6", side_effect=no_srp6):
            with self.assertRaises(AuthRejected) as cm:
                auth._handshake(sock, USERNAME, PASSWORD)
        err = cm.exception
        self.assertIsInstance(err, AuthSecurityUnsupported)
        self.assertEqual(err.code, 0x04)
        self.assertEqual(err.flags, 0x05)
        self.assertIn("PIN", str(err))
        self.assertIn("authenticator token", str(err))
        self.assertNotIn("matrix", str(err))
        self.assertIn("0x05", str(err))

    def test_message_never_contains_the_username_or_password(self):
        sock = FakeSocket(challenge(0x01))
        with mock.patch.object(auth, "compute_srp6", side_effect=no_srp6):
            with self.assertRaises(AuthSecurityUnsupported) as cm:
                auth._handshake(sock, USERNAME, PASSWORD)
        msg = str(cm.exception).lower()
        self.assertNotIn(USERNAME, msg)
        self.assertNotIn(PASSWORD, msg)

    def test_flags_zero_still_sends_the_proof(self):
        sock = FakeSocket(challenge(0x00), PROOF_REJECT)
        fake_srp6 = mock.Mock(return_value=(b"A" * 32, b"M" * 20, b"C" * 20, b"K" * 40))
        with mock.patch.object(auth, "compute_srp6", fake_srp6):
            with self.assertRaises(AuthRejected) as cm:
                auth._handshake(sock, USERNAME, PASSWORD)
        self.assertNotIsInstance(cm.exception, AuthSecurityUnsupported)
        fake_srp6.assert_called_once()
        self.assertEqual(sock.sent[1][0], 0x01)  # LOGON_PROOF was sent


class TruncatedReplyTest(unittest.TestCase):
    """#517: the challenge and realm-list parsers read through wire.Reader, so a reply whose
    lengths run past its bytes raises PacketTruncated at the failed read, not IndexError."""

    def test_challenge_with_a_group_length_past_the_reply_is_truncated(self):
        bad = bytearray(challenge(0x00))
        bad[35] = 0xFF  # g_len: 255 bytes claimed, 83 left in the 119-byte reply
        with mock.patch.object(auth, "compute_srp6", side_effect=no_srp6):
            with self.assertRaises(PacketTruncated) as cm:
                auth._handshake(FakeSocket(bytes(bad)), USERNAME, PASSWORD)
        self.assertEqual((cm.exception.offset, cm.exception.wanted), (36, 255))

    def test_realm_list_with_a_realm_missing_its_fields_is_truncated(self):
        srp = mock.Mock(return_value=(b"A" * 32, b"M" * 20, b"C" * 20, b"K" * 40))
        body = struct.pack('<IH', 0, 1)  # unknown uint32, one realm, but no realm record
        sock = FakeSocket(challenge(0x00), bytes([0x01, 0x00]) + bytes(30),
                          bytes([0x10]) + struct.pack('<H', len(body)) + body)
        with mock.patch.object(auth, "compute_srp6", srp):
            with self.assertRaises(PacketTruncated) as cm:
                auth._handshake(sock, USERNAME, PASSWORD)
        self.assertEqual((cm.exception.offset, cm.exception.wanted), (6, 1))


class SupervisorSecurityFlagsTest(unittest.TestCase):
    def test_supervisor_does_not_retry_a_security_flag_refusal(self):
        attempts, lines = [], []

        class Log:
            def info(self, *a): pass
            def warning(self, *a): lines.append(("warning", a))
            def error(self, *a): lines.append(("error", a))
            def exception(self, *a): lines.append(("exception", a))

        def build_session():
            attempts.append(1)
            raise AuthSecurityUnsupported(0x01)

        def no_sleep(seconds):
            raise AssertionError(f"supervisor retried after a permanent refusal: sleep {seconds}")

        with self.assertRaises(AuthSecurityUnsupported):
            main_mod.supervise_connection(build_session, lambda s: False, log=Log(),
                                           sleep=no_sleep)
        self.assertEqual(len(attempts), 1)
        self.assertEqual([level for level, _ in lines], ["error"])


if __name__ == "__main__":
    unittest.main()
