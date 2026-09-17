"""Unit tests for agent.actions: chat wire format, language selection,
message encoding, and the social action opcodes/payloads."""

import math
import struct
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import movement as mv
from agent import packets as pk
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo


def fake_session(race=10, player_guid=0xF130000000000099, player_position=None):
    """race defaults to 10 (Blood Elf, Horde)."""
    sent = []
    sess = SimpleNamespace(race=race, pending_invite={"inviter_name": "Rubens"},
                            player_guid=player_guid, player_position=player_position)
    sess._send_packet = lambda opcode, payload=b'': sent.append((opcode, payload))
    sess._sent = sent
    return sess


def object_at(guid, x, y, z, object_type="unit"):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT,
        guid=guid,
        object_type=uo.TYPEID_PLAYER if object_type == "player" else uo.TYPEID_UNIT,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={},
    )


class LanguageSelectionTest(unittest.TestCase):
    def test_horde_race_uses_orcish(self):
        self.assertEqual(ac._racial_language(fake_session(race=10)), ac.LANG_ORCISH)  # Blood Elf
        self.assertEqual(ac._racial_language(fake_session(race=2)), ac.LANG_ORCISH)   # Orc

    def test_alliance_race_uses_common(self):
        self.assertEqual(ac._racial_language(fake_session(race=1)), ac.LANG_COMMON)   # Human
        self.assertEqual(ac._racial_language(fake_session(race=11)), ac.LANG_COMMON)  # Draenei

    def test_unknown_race_defaults_to_orcish(self):
        self.assertEqual(ac._racial_language(fake_session(race=0)), ac.LANG_ORCISH)

    def test_never_sends_universal(self):
        for race in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11):
            with self.subTest(race=race):
                self.assertNotEqual(ac._racial_language(fake_session(race=race)), ac.LANG_UNIVERSAL)


class MessageEncodingTest(unittest.TestCase):
    def test_utf8_accents_preserved(self):
        encoded = ac._encode_message("Olá, tudo bem?")
        self.assertEqual(encoded.decode("utf-8"), "Olá, tudo bem?")

    def test_newlines_stripped(self):
        self.assertNotIn(b'\n', ac._encode_message("line one\nline two"))
        self.assertNotIn(b'\r', ac._encode_message("a\r\nb"))

    def test_capped_at_255_bytes(self):
        encoded = ac._encode_message("x" * 500)
        self.assertLessEqual(len(encoded), 255)

    def test_cap_does_not_split_multibyte_char(self):
        # 'á' is 2 bytes in UTF-8; pad so the cut would land mid-character.
        message = "x" * 254 + "á"
        encoded = ac._encode_message(message)
        encoded.decode("utf-8")  # raises UnicodeDecodeError if truncated mid-sequence


class ChatWireFormatTest(unittest.TestCase):
    def _decode_chat_payload(self, payload, expect_target=False):
        slash_cmd, lang = struct.unpack_from('<ii', payload, 0)
        off = 8
        target = None
        if expect_target:
            end = payload.index(b'\x00', off)
            target = payload[off:end].decode('utf-8')
            off = end + 1
        end = payload.index(b'\x00', off)
        text = payload[off:end].decode('utf-8')
        return slash_cmd, lang, target, text

    def test_say(self):
        sess = fake_session()
        ac.say(sess, "hello")
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)
        slash_cmd, lang, target, text = self._decode_chat_payload(payload)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_SAY)
        self.assertEqual(lang, ac.LANG_ORCISH)
        self.assertIsNone(target)
        self.assertEqual(text, "hello")

    def test_yell(self):
        sess = fake_session()
        ac.yell(sess, "for the horde")
        _, payload = sess._sent[0]
        slash_cmd, _, _, text = self._decode_chat_payload(payload)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_YELL)
        self.assertEqual(text, "for the horde")

    def test_whisper_includes_real_target(self):
        sess = fake_session()
        ac.whisper(sess, "Rubens", "Olá!")
        _, payload = sess._sent[0]
        slash_cmd, _, target, text = self._decode_chat_payload(payload, expect_target=True)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_WHISPER)
        self.assertEqual(target, "Rubens")
        self.assertEqual(text, "Olá!")

    def test_emote(self):
        sess = fake_session()
        ac.emote(sess, "waves")
        _, payload = sess._sent[0]
        slash_cmd, _, _, text = self._decode_chat_payload(payload)
        self.assertEqual(slash_cmd, ac.CHAT_MSG_EMOTE)
        self.assertEqual(text, "waves")

    def test_text_emote(self):
        sess = fake_session()
        ac.text_emote(sess, 66, target_guid=5)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_TEXT_EMOTE)
        emote_id, sound, guid = struct.unpack('<iiQ', payload)
        self.assertEqual(emote_id, 66)
        self.assertEqual(sound, 0)
        self.assertEqual(guid, 5)


class GroupActionsTest(unittest.TestCase):
    def test_invite_to_group(self):
        sess = fake_session()
        ac.invite_to_group(sess, "Rubens")
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_INVITE)
        end = payload.index(b'\x00')
        self.assertEqual(payload[:end].decode(), "Rubens")
        roles = struct.unpack_from('<I', payload, end + 1)[0]
        self.assertEqual(roles, 0)

    def test_accept_group_clears_pending_invite(self):
        sess = fake_session()
        self.assertIsNotNone(sess.pending_invite)
        ac.accept_group(sess)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_ACCEPT)
        self.assertIsNone(sess.pending_invite)

    def test_leave_group_sends_no_payload(self):
        sess = fake_session()
        ac.leave_group(sess)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_DISBAND)
        self.assertEqual(payload, b'')


class SendChatMessageBackCompatTest(unittest.TestCase):
    def test_whisper_without_target_raises(self):
        sess = fake_session()
        with self.assertRaises(ValueError):
            ac.send_chat_message(sess, "hi", channel="whisper")

    def test_channel_dispatch(self):
        sess = fake_session()
        ac.send_chat_message(sess, "hi", channel="yell")
        _, payload = sess._sent[0]
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_YELL)


class SendAttackTest(unittest.TestCase):
    def test_attackswing_carries_target_guid(self):
        # Found in review: CMSG_ATTACKSWING was sent with no payload at all,
        # but AttackSwing::Read (CombatPackets.cpp) reads a raw uint64 guid.
        sess = fake_session()
        ac.send_attack(sess, 0x1234)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.CMSG_ATTACKSWING, opcodes)
        payload = dict(sess._sent)[ac.CMSG_ATTACKSWING]
        self.assertEqual(payload, struct.pack("<Q", 0x1234))
        self.assertNotEqual(payload, b'')


class ActionFrameworkTest(unittest.TestCase):
    def test_registry_has_set_target_and_face(self):
        self.assertIn("set_target", ac.REGISTRY)
        self.assertIn("face", ac.REGISTRY)

    def test_catalog_returns_valid_schema_for_every_action(self):
        for schema in ac.catalog():
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIsInstance(params["properties"], dict)
            self.assertIsInstance(params["required"], list)
            for req in params["required"]:
                self.assertIn(req, params["properties"])

    def test_run_short_circuits_on_check_failure_without_calling_execute(self):
        calls = []

        class Failing(ac.Action):
            name = "failing"

            def check(self, session, world, **params):
                return "nope"

            def execute(self, session, world, **params):
                calls.append(1)
                return ac.ActionResult(ok=True)

        result = Failing().run(None, None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "nope")
        self.assertEqual(calls, [])

    def test_wait_for_returns_true_once_predicate_flips(self):
        calls = {"n": 0}

        def predicate():
            calls["n"] += 1
            return calls["n"] >= 3  # false on the first two polls, true on the third

        self.assertTrue(ac._wait_for(predicate, timeout=1.0, interval=0.01))
        self.assertGreaterEqual(calls["n"], 3)

    def test_wait_for_times_out(self):
        self.assertFalse(ac._wait_for(lambda: False, timeout=0.05, interval=0.01))


class SetTargetActionTest(unittest.TestCase):
    def test_check_fails_for_unperceived_guid(self):
        world = per.WorldState()
        err = ac.SetTargetAction().check(None, world, guid=5)
        self.assertIsNotNone(err)

    def test_check_passes_for_perceived_guid(self):
        world = per.WorldState()
        world.update_object(object_at(5, 1.0, 2.0, 3.0))
        err = ac.SetTargetAction().check(None, world, guid=5)
        self.assertIsNone(err)

    def test_execute_times_out_when_target_field_never_updates(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(5, 1.0, 2.0, 3.0))
        sess = fake_session()

        action = ac.SetTargetAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)

        self.assertIn(ac.CMSG_SET_SELECTION, [op for op, _ in sess._sent])
        self.assertFalse(result.ok)  # nothing ever set target_guid in this fake world

    def test_execute_confirms_once_target_field_updates(self):
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        world.update_object(object_at(5, 1.0, 2.0, 3.0))
        sess = fake_session()

        # UNIT_FIELD_TARGET (a guid-typed field spanning two slots) already
        # set before execute() runs — simplest way to exercise the "found on
        # the first poll" path without a real background thread in the test.
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=1,
                                            fields={uf.UNIT_FIELD_TARGET: 5, uf.UNIT_FIELD_TARGET + 1: 0}))
        self.assertEqual(world.get_my_object().target_guid, 5)

        action = ac.SetTargetAction()
        action.confirm_timeout = 0.5
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)
        self.assertTrue(result.ok)


class FaceActionTest(unittest.TestCase):
    def test_check_requires_guid_or_xy(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world))

    def test_check_rejects_both_guid_and_xy(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world, guid=1, x=1.0, y=1.0))

    def test_check_requires_own_position(self):
        sess = fake_session(player_position=None)
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world, x=1.0, y=1.0))

    def test_check_requires_known_target_position(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.FaceAction().check(sess, world, guid=99))

    def test_execute_faces_east_toward_xy(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 3.14))
        world = per.WorldState()
        result = ac.FaceAction().execute(sess, world, x=10.0, y=0.0)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.detail["orientation"], 0.0, places=4)
        self.assertAlmostEqual(sess.player_position[4], 0.0, places=4)
        self.assertEqual(sess.player_position[1:4], (0.0, 0.0, 0.0))  # position unchanged, only facing

    def test_execute_faces_toward_a_guid(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 0.0, 10.0, 0.0))  # due north (+y)
        result = ac.FaceAction().execute(sess, world, guid=5)
        self.assertAlmostEqual(result.detail["orientation"], math.pi / 2, places=4)

    def test_execute_sends_msg_move_set_facing(self):
        sess = fake_session(player_guid=0x42, player_position=(530, 1.0, 2.0, 3.0, 0.0))
        world = per.WorldState()
        ac.FaceAction().execute(sess, world, x=11.0, y=2.0)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(mv.MSG_MOVE_SET_FACING, opcodes)
        _, payload = sess._sent[0]
        guid, off = pk.unpack_packed_guid(payload, 0)
        self.assertEqual(guid, 0x42)


if __name__ == '__main__':
    unittest.main()
