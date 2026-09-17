"""Unit tests for agent.actions: chat wire format, language selection,
message encoding, and the social action opcodes/payloads."""

import math
import struct
import threading
import time
import unittest
from types import SimpleNamespace

from agent import actions as ac
from agent import movement as mv
from agent import npc
from agent import packets as pk
from agent import perception as per
from agent import trade as tr
from agent import update_fields as uf
from agent import update_object as uo


def fake_session(race=10, player_guid=0xF130000000000099, player_position=None, class_=0):
    """race defaults to 10 (Blood Elf, Horde)."""
    sent = []
    sess = SimpleNamespace(race=race, class_=class_, pending_invite={"inviter_name": "Rubens"},
                            player_guid=player_guid, player_position=player_position,
                            events=[], spellbook=set(), spell_cooldowns={})
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


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, dt: float):
        self.t += dt


def fast_session(guid=0xF130000000000099, position=(530, 0.0, 0.0, 0.0, 0.0)):
    """A fake_session pre-wired with a Mover on a FakeClock, so move_to/
    move_towards/stop_movement actions run instantly in tests instead of
    sleeping for real between ticks."""
    sess = fake_session(player_guid=guid, player_position=position)
    world = per.WorldState()
    clock = FakeClock()
    sess._mover = mv.Mover(sess, world, clock=clock.clock, sleep=clock.sleep)
    return sess, world


class MoveToActionTest(unittest.TestCase):
    def test_check_requires_own_position(self):
        sess = fake_session(player_position=None)
        world = per.WorldState()
        self.assertIsNotNone(ac.MoveToAction().check(sess, world, x=1.0, y=1.0))

    def test_execute_arrives_and_returns_ok(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        result = ac.MoveToAction().execute(sess, world, x=5.0, y=0.0, stop_distance=1.0)
        self.assertTrue(result.ok)
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes[0], mv.MSG_MOVE_START_FORWARD)
        self.assertEqual(opcodes[-1], mv.MSG_MOVE_STOP)

    def test_execute_reuses_the_same_mover_as_stop_movement(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        ac.MoveToAction().execute(sess, world, x=5.0, y=0.0)
        self.assertIs(sess._mover, mv.get_mover(sess, world))


class MoveTowardsActionTest(unittest.TestCase):
    def test_check_fails_when_target_has_no_position(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.MoveTowardsAction().check(sess, world, guid=99))

    def test_execute_chases_and_arrives(self):
        sess, world = fast_session(position=(530, 0.0, 0.0, 0.0, 0.0))
        world.update_object(object_at(5, 5.0, 0.0, 0.0))
        result = ac.MoveTowardsAction().execute(sess, world, guid=5, stop_distance=1.0)
        self.assertTrue(result.ok)


class StopMovementActionTest(unittest.TestCase):
    def test_returns_ok_with_was_moving_false_when_idle(self):
        sess, world = fast_session()
        result = ac.StopMovementAction().execute(sess, world)
        self.assertTrue(result.ok)
        self.assertFalse(result.detail["was_moving"])


def append_event_after(sess, delay, event):
    """Appends `event` (with a fresh t=time.monotonic()) to sess.events
    after `delay` seconds, on a background thread — used to satisfy an
    action's _wait_for/_wait_for_value confirmation loop from outside the
    single-threaded execute() call under test."""
    def worker():
        time.sleep(delay)
        sess.events.append({**event, "t": time.monotonic()})
    t = threading.Thread(target=worker)
    t.start()
    return t


class AutoAttackActionTest(unittest.TestCase):
    def test_check_target_not_perceived(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.AutoAttackAction().check(sess, world, guid=5))

    def test_check_target_already_dead(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.update_object(object_at(5, 1.0, 0.0, 0.0))
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=5,
                                            fields={uf.UNIT_FIELD_HEALTH: 0}))
        self.assertIsNotNone(ac.AutoAttackAction().check(sess, world, guid=5))

    def test_check_out_of_melee_range_suggests_move_towards(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 100.0, 0.0, 0.0))
        err = ac.AutoAttackAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("move_towards", err)

    def test_check_passes_within_melee_range(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 3.0, 0.0, 0.0))
        self.assertIsNone(ac.AutoAttackAction().check(sess, world, guid=5))

    def test_execute_sends_target_and_attackswing(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 3.0, 0.0, 0.0))
        action = ac.AutoAttackAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.CMSG_SET_SELECTION, opcodes)
        self.assertIn(ac.CMSG_ATTACKSWING, opcodes)
        self.assertFalse(result.ok)  # no attack_start event ever arrived

    def test_execute_confirms_via_attack_start_event(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.update_object(object_at(5, 3.0, 0.0, 0.0))
        action = ac.AutoAttackAction()
        action.confirm_timeout = 1.0
        action.confirm_interval = 0.02

        t = append_event_after(sess, 0.05, {"kind": "attack_start", "victim_guid": 5})
        result = action.execute(sess, world, guid=5)
        t.join()
        self.assertTrue(result.ok)


class StopAttackActionTest(unittest.TestCase):
    def test_sends_attackstop_with_no_payload(self):
        sess = fake_session()
        world = per.WorldState()
        result = ac.StopAttackAction().execute(sess, world)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_ATTACKSTOP)
        self.assertEqual(payload, b'')


class CastSpellActionTest(unittest.TestCase):
    def test_check_unknown_spell(self):
        sess = fake_session()
        world = per.WorldState()
        self.assertIsNotNone(ac.CastSpellAction().check(sess, world, spell_id=635))

    def test_check_known_spell_passes(self):
        sess = fake_session()
        sess.spellbook.add(635)
        world = per.WorldState()
        self.assertIsNone(ac.CastSpellAction().check(sess, world, spell_id=635))

    def test_check_on_cooldown(self):
        sess = fake_session()
        sess.spellbook.add(635)
        sess.spell_cooldowns[635] = {"recovery_time": 1000}
        world = per.WorldState()
        self.assertIsNotNone(ac.CastSpellAction().check(sess, world, spell_id=635))

    def test_check_not_enough_power(self):
        sess = fake_session()
        sess.spellbook.add(635)  # Holy Light: costs 25 mana
        world = per.WorldState()
        world.set_my_guid(1)
        world.update_object(object_at(1, 0.0, 0.0, 0.0, object_type="player"))
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=1,
                                            fields={uf.UNIT_FIELD_POWER1: 5}))
        err = ac.CastSpellAction().check(sess, world, spell_id=635)
        self.assertIsNotNone(err)

    def test_check_unknown_target_guid(self):
        sess = fake_session()
        sess.spellbook.add(133)
        world = per.WorldState()
        self.assertIsNotNone(ac.CastSpellAction().check(sess, world, spell_id=133, target_guid=99))

    def test_execute_sends_cast_and_times_out_without_response(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(21084)
        world = per.WorldState()
        action = ac.CastSpellAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, spell_id=21084)
        self.assertIn(ac.CMSG_CAST_SPELL, [op for op, _ in sess._sent])
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    def test_execute_confirms_success_via_spell_go(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(21084)
        world = per.WorldState()
        action = ac.CastSpellAction()
        action.confirm_timeout = 1.0
        action.confirm_interval = 0.02

        t = append_event_after(sess, 0.05, {"kind": "spell_go", "spell_id": 21084})
        result = action.execute(sess, world, spell_id=21084)
        t.join()
        self.assertTrue(result.ok)

    def test_execute_reports_cast_failed_reason(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(21084)
        world = per.WorldState()
        action = ac.CastSpellAction()
        action.confirm_timeout = 1.0
        action.confirm_interval = 0.02

        t = append_event_after(sess, 0.05,
                                {"kind": "cast_failed", "spell_id": 21084, "reason_name": "out_of_range"})
        result = action.execute(sess, world, spell_id=21084)
        t.join()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "out_of_range")

    def test_execute_extends_wait_by_reported_cast_time(self):
        """A cast-time spell's SMSG_SPELL_GO only arrives after cast_time
        (ms) elapses — found live testing Holy Light (2.5s cast) against a
        training dummy, which timed out on a fixed confirm_timeout even
        though the cast succeeded. confirm_timeout alone must not be the
        deadline once SMSG_SPELL_START reports a real cast_time."""
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(635)
        world = per.WorldState()
        action = ac.CastSpellAction()
        action.confirm_timeout = 0.1
        action.confirm_interval = 0.02

        append_event_after(sess, 0.02, {"kind": "spell_start", "spell_id": 635, "cast_time": 300})
        t = append_event_after(sess, 0.25, {"kind": "spell_go", "spell_id": 635})
        result = action.execute(sess, world, spell_id=635)
        t.join()
        self.assertTrue(result.ok)

    def test_execute_reports_cast_failed_after_cast_time_wait(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(635)
        world = per.WorldState()
        action = ac.CastSpellAction()
        action.confirm_timeout = 0.1
        action.confirm_interval = 0.02

        append_event_after(sess, 0.02, {"kind": "spell_start", "spell_id": 635, "cast_time": 300})
        t = append_event_after(sess, 0.25,
                                {"kind": "cast_failed", "spell_id": 635, "reason_name": "interrupted"})
        result = action.execute(sess, world, spell_id=635)
        t.join()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "interrupted")

    def test_execute_faces_target_first(self):
        sess = fake_session(player_guid=0x42, player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(133)
        world = per.WorldState()
        world.update_object(object_at(5, 10.0, 0.0, 0.0))
        action = ac.CastSpellAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        action.execute(sess, world, spell_id=133, target_guid=5)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(mv.MSG_MOVE_SET_FACING, opcodes)
        self.assertIn(ac.CMSG_CAST_SPELL, opcodes)

    def test_execute_self_cast_sends_target_flag_none(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        sess.spellbook.add(21084)
        world = per.WorldState()
        action = ac.CastSpellAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        action.execute(sess, world, spell_id=21084)
        opcode, payload = next(p for p in sess._sent if p[0] == ac.CMSG_CAST_SPELL)
        cast_id, spell_id, flags = struct.unpack_from('<BiB', payload, 0)
        target_flags = struct.unpack_from('<I', payload, 6)[0]
        self.assertEqual(spell_id, 21084)
        self.assertEqual(target_flags, 0)  # TARGET_FLAG_NONE


def npc_object(guid, x, y, z, npc_flags=0, object_type="unit"):
    """A creature/gameobject ObjectInfo with npc_flags set via a VALUES
    merge, mirroring how real SMSG_UPDATE_OBJECT blocks land (CREATE with
    movement, then a fields-only merge)."""
    world_obj_type = uo.TYPEID_GAMEOBJECT if object_type == "gameobject" else uo.TYPEID_UNIT
    block = uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=world_obj_type,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": x, "y": y, "z": z, "o": 0.0},
        fields={uf.UNIT_NPC_FLAGS: npc_flags} if object_type != "gameobject" else {},
    )
    return block


class InteractActionTest(unittest.TestCase):
    def test_check_fails_for_unperceived_guid(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.InteractAction().check(sess, world, guid=5))

    def test_check_out_of_range_suggests_move_towards(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 100.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_VENDOR))
        err = ac.InteractAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("move_towards", err)

    def test_execute_sends_gossip_hello_for_gossip_flag(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_GOSSIP))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_GOSSIP_HELLO, npc.build_gossip_hello(5)))

    def test_execute_sends_list_inventory_for_vendor_only_flag(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_VENDOR))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_LIST_INVENTORY, npc.build_list_inventory(5)))

    def test_execute_sends_trainer_list_for_trainer_only_flag(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=per.UNIT_NPC_FLAG_TRAINER))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_TRAINER_LIST, npc.build_trainer_list(5)))

    def test_execute_sends_gameobj_use_for_gameobject(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, object_type="gameobject"))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_GAMEOBJ_USE, npc.build_gameobj_use(5)))

    def test_execute_reports_no_interaction_for_plain_unit(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0, npc_flags=0))
        result = ac.InteractAction().execute(sess, world, guid=5)
        self.assertFalse(result.ok)


class GossipSelectActionTest(unittest.TestCase):
    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.GossipSelectAction().check(fake_session(), world, option_index=0))

    def test_check_fails_for_unknown_option(self):
        world = per.WorldState()
        world.ui_state = {"kind": "gossip", "npc_guid": 5, "menu_id": 1,
                           "options": [{"index": 0}]}
        self.assertIsNotNone(ac.GossipSelectAction().check(fake_session(), world, option_index=9))

    def test_execute_sends_select_option(self):
        world = per.WorldState()
        world.ui_state = {"kind": "gossip", "npc_guid": 5, "menu_id": 1,
                           "options": [{"index": 0}, {"index": 1}]}
        sess = fake_session()
        result = ac.GossipSelectAction().execute(sess, world, option_index=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_GOSSIP_SELECT_OPTION,
                                          npc.build_gossip_select_option(5, 1, 1)))


class BuyItemActionTest(unittest.TestCase):
    def _window(self):
        return {"kind": "vendor", "vendor_guid": 5,
                "items": [{"slot": 1, "entry": 6948, "price": 500},
                          {"slot": 2, "entry": 159, "price": 10}]}

    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.BuyItemAction().check(fake_session(), world, vendor_guid=5, slot=1))

    def test_check_fails_for_unknown_slot(self):
        world = per.WorldState()
        world.ui_state = self._window()
        self.assertIsNotNone(ac.BuyItemAction().check(fake_session(), world, vendor_guid=5, slot=9))

    def test_execute_by_slot_succeeds_on_buy_item_ack(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "buy_item", "vendor_guid": 5, "slot": 2,
                                         "new_count": 3, "stacks": 3})
        result = action.execute(sess, world, vendor_guid=5, slot=2, count=3)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_BUY_ITEM, npc.build_buy_item(5, 159, 2, 3)))

    def test_execute_by_entry_succeeds_on_buy_item_ack(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "buy_item", "vendor_guid": 5, "slot": 1,
                                         "new_count": -1, "stacks": 1})
        result = action.execute(sess, world, vendor_guid=5, entry=6948, count=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_BUY_ITEM, npc.build_buy_item(5, 6948, 1, 1)))

    def test_execute_fails_on_buy_failed(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "buy_failed", "vendor_guid": 5, "item_entry": 159,
                                         "reason": 2, "reason_name": "not_enough_money"})
        result = action.execute(sess, world, vendor_guid=5, slot=2, count=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_enough_money")

    def test_execute_no_response_times_out_as_failure(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.BuyItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, vendor_guid=5, slot=2, count=1)
        self.assertFalse(result.ok)


class SellItemActionTest(unittest.TestCase):
    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.SellItemAction().check(fake_session(), world, vendor_guid=5, bag=255, slot=23))

    def test_check_fails_for_unknown_item(self):
        world = per.WorldState()
        world.ui_state = {"kind": "vendor", "vendor_guid": 5, "items": []}
        self.assertIsNotNone(ac.SellItemAction().check(fake_session(), world, vendor_guid=5, bag=255, slot=23))

    def test_execute_succeeds_when_no_failure_arrives(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.ui_state = {"kind": "vendor", "vendor_guid": 5, "items": []}
        sess = fake_session()
        action = ac.SellItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, vendor_guid=5, bag=255, slot=23, count=1)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_SELL_ITEM, npc.build_sell_item(5, item_guid, 1)))

    def test_execute_fails_on_sell_failed(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.ui_state = {"kind": "vendor", "vendor_guid": 5, "items": []}
        sess = fake_session()
        action = ac.SellItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "sell_failed", "vendor_guid": 5, "item_guid": item_guid,
                                         "reason": 2, "reason_name": "cant_sell_item"})
        result = action.execute(sess, world, vendor_guid=5, bag=255, slot=23, count=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "cant_sell_item")


class TrainSpellActionTest(unittest.TestCase):
    def _window(self):
        return {"kind": "trainer", "trainer_guid": 5, "spells": [{"spell_id": 587}]}

    def test_check_fails_without_open_window(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.TrainSpellAction().check(fake_session(), world,
                                                          trainer_guid=5, spell_id=587))

    def test_check_fails_for_unknown_spell(self):
        world = per.WorldState()
        world.ui_state = self._window()
        self.assertIsNotNone(ac.TrainSpellAction().check(fake_session(), world,
                                                          trainer_guid=5, spell_id=999))

    def test_execute_succeeds_on_train_succeeded(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.TrainSpellAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "train_succeeded", "trainer_guid": 5, "spell_id": 587})
        result = action.execute(sess, world, trainer_guid=5, spell_id=587)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (npc.CMSG_TRAINER_BUY_SPELL, npc.build_train_spell(5, 587)))

    def test_execute_fails_on_train_failed(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.TrainSpellAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "train_failed", "trainer_guid": 5, "spell_id": 587,
                                         "reason": 1, "reason_name": "not_enough_money"})
        result = action.execute(sess, world, trainer_guid=5, spell_id=587)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_enough_money")

    def test_execute_no_response_times_out(self):
        world = per.WorldState()
        world.ui_state = self._window()
        sess = fake_session()
        action = ac.TrainSpellAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, trainer_guid=5, spell_id=587)
        self.assertFalse(result.ok)


class CloseWindowActionTest(unittest.TestCase):
    def test_closes_open_window(self):
        world = per.WorldState()
        world.ui_state = {"kind": "gossip"}
        result = ac.CloseWindowAction().execute(fake_session(), world)
        self.assertTrue(result.ok)
        self.assertTrue(result.detail["was_open"])
        self.assertIsNone(world.get_ui_state())

    def test_noop_when_already_closed(self):
        world = per.WorldState()
        result = ac.CloseWindowAction().execute(fake_session(), world)
        self.assertTrue(result.ok)
        self.assertFalse(result.detail["was_open"])


class SocialActionsRegistrationTest(unittest.TestCase):
    def test_registry_has_all_social_actions(self):
        for name in ("say", "yell", "whisper", "emote", "invite_to_group", "accept_group"):
            self.assertIn(name, ac.REGISTRY)


class SayActionTest(unittest.TestCase):
    def test_execute_sends_say(self):
        sess = fake_session()
        result = ac.SayAction().execute(sess, None, message="hello")
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_SAY)


class YellActionTest(unittest.TestCase):
    def test_execute_sends_yell(self):
        sess = fake_session()
        result = ac.YellAction().execute(sess, None, message="for the horde")
        self.assertTrue(result.ok)
        _, payload = sess._sent[0]
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_YELL)


class EmoteActionTest(unittest.TestCase):
    def test_execute_sends_emote(self):
        sess = fake_session()
        result = ac.EmoteAction().execute(sess, None, text="waves")
        self.assertTrue(result.ok)
        _, payload = sess._sent[0]
        slash_cmd = struct.unpack_from('<i', payload, 0)[0]
        self.assertEqual(slash_cmd, ac.CHAT_MSG_EMOTE)


class InviteToGroupActionTest(unittest.TestCase):
    def test_execute_succeeds_when_no_failure_arrives(self):
        sess = fake_session()
        action = ac.InviteToGroupAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, None, name="Rubens")
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_INVITE)

    def test_execute_fails_on_group_invite_failed(self):
        sess = fake_session()
        action = ac.InviteToGroupAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "group_invite_failed", "target_name": "Rubens",
                                         "result": 5, "result_name": "already_in_group"})
        result = action.execute(sess, None, name="Rubens")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "already_in_group")


class WhisperActionTest(unittest.TestCase):
    def test_check_fails_for_unknown_name(self):
        world = per.WorldState()
        err = ac.WhisperAction().check(fake_session(), world, target_name="Nobody", message="hi")
        self.assertIsNotNone(err)

    def test_check_passes_for_perceived_player_name(self):
        world = per.WorldState()
        world.update_object(object_at(5, 1.0, 2.0, 3.0, object_type="player"))
        world.objects[5].name = "Rubens"
        err = ac.WhisperAction().check(fake_session(), world, target_name="Rubens", message="hi")
        self.assertIsNone(err)

    def test_check_passes_for_recent_chat_sender(self):
        world = per.WorldState()
        sess = fake_session()
        sess.chat_inbox = [{"sender_name": "Rubens", "text": "hi", "kind": "say"}]
        err = ac.WhisperAction().check(sess, world, target_name="rubens", message="hi")
        self.assertIsNone(err)

    def test_execute_sends_whisper(self):
        world = per.WorldState()
        sess = fake_session()
        sess.chat_inbox = [{"sender_name": "Rubens", "text": "hi", "kind": "say"}]
        action = ac.WhisperAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.run(sess, world, target_name="Rubens", message="hi there")
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_MESSAGECHAT)

    def test_execute_fails_on_whisper_failed(self):
        world = per.WorldState()
        sess = fake_session()
        sess.chat_inbox = [{"sender_name": "Rubens", "text": "hi", "kind": "say"}]
        action = ac.WhisperAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "whisper_failed", "target_name": "Rubens"})
        result = action.run(sess, world, target_name="Rubens", message="hi there")
        self.assertFalse(result.ok)
        self.assertIn("Rubens", result.error)


class AcceptGroupActionTest(unittest.TestCase):
    def test_check_fails_without_pending_invite(self):
        sess = fake_session()
        sess.pending_invite = None
        err = ac.AcceptGroupAction().check(sess, None)
        self.assertIsNotNone(err)

    def test_check_passes_with_pending_invite(self):
        sess = fake_session()
        err = ac.AcceptGroupAction().check(sess, None)
        self.assertIsNone(err)

    def test_execute_sends_accept_and_clears_pending_invite(self):
        sess = fake_session()
        result = ac.AcceptGroupAction().run(sess, None)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.CMSG_GROUP_ACCEPT)
        self.assertIsNone(sess.pending_invite)
        self.assertEqual(result.detail["inviter_name"], "Rubens")


def lootable_object_at(guid, x, y, z):
    block = object_at(guid, x, y, z)
    ws_block = uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=guid,
                               fields={uf.UNIT_DYNAMIC_FLAGS: per.UNIT_DYNFLAG_LOOTABLE})
    return block, ws_block


def item_object(guid, entry, count=1):
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_ITEM,
        movement={"update_flags": 0},
        fields={uf.OBJECT_FIELD_ENTRY: entry, uf.ITEM_FIELD_STACK_COUNT: count},
    )


def self_player_with_slots(guid, slot_guids):
    raw = {}
    for slot, item_guid in slot_guids.items():
        if slot < 23:
            base = uf.PLAYER_FIELD_INV_SLOT_HEAD + slot * 2
        else:
            base = uf.PLAYER_FIELD_PACK_SLOT_1 + (slot - 23) * 2
        raw[base] = item_guid & 0xFFFFFFFF
        raw[base + 1] = item_guid >> 32
    return uo.UpdateBlock(
        update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=guid, object_type=uo.TYPEID_PLAYER,
        movement={"update_flags": uo.UPDATEFLAG_STATIONARY_POSITION, "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0},
        fields=raw,
    )


class LootActionTest(unittest.TestCase):
    def test_check_not_lootable(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 1.0, 0.0, 0.0))
        self.assertIsNotNone(ac.LootAction().check(sess, world, guid=5))

    def test_check_out_of_range(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        block, ws_block = lootable_object_at(5, 100.0, 0.0, 0.0)
        world.update_object(block)
        world.update_object(ws_block)
        err = ac.LootAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("loot range", err)

    def test_check_passes_in_range_and_lootable(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        block, ws_block = lootable_object_at(5, 2.0, 0.0, 0.0)
        world.update_object(block)
        world.update_object(ws_block)
        self.assertIsNone(ac.LootAction().check(sess, world, guid=5))

    def test_execute_full_sequence_on_success(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        action = ac.LootAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01

        response = {"kind": "loot_response", "guid": 5, "success": True, "coins": 12,
                    "items": [{"slot": 0, "entry": 159, "count": 4}]}
        append_event_after(sess, 0.02, response)
        result = action.execute(sess, world, guid=5)

        opcodes = [op for op, _ in sess._sent]
        self.assertIn(ac.lootmod.CMSG_LOOT, opcodes)
        self.assertIn(ac.lootmod.CMSG_LOOT_MONEY, opcodes)
        self.assertIn(ac.lootmod.CMSG_AUTOSTORE_LOOT_ITEM, opcodes)
        self.assertIn(ac.lootmod.CMSG_LOOT_RELEASE, opcodes)
        # release must be the very last packet sent
        self.assertEqual(opcodes[-1], ac.lootmod.CMSG_LOOT_RELEASE)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["coins"], 12)
        self.assertEqual(result.detail["items"], response["items"])

    def test_execute_no_response_times_out(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        action = ac.LootAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, guid=5)
        self.assertFalse(result.ok)
        # never got a response, so no follow-up loot packets were sent
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes, [ac.lootmod.CMSG_LOOT])

    def test_execute_failure_response_stops_early(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        action = ac.LootAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "loot_response", "guid": 5, "success": False,
                                          "failure_reason_name": "too_far"})
        result = action.execute(sess, world, guid=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "too_far")
        opcodes = [op for op, _ in sess._sent]
        self.assertEqual(opcodes, [ac.lootmod.CMSG_LOOT])


class UseItemActionTest(unittest.TestCase):
    def test_check_no_item_at_slot(self):
        sess = fake_session()
        world = per.WorldState()
        world.set_my_guid(0x1)
        world.update_object(self_player_with_slots(0x1, {}))
        self.assertIsNotNone(ac.UseItemAction().check(sess, world, bag=255, slot=23))

    def test_check_blocked_in_combat(self):
        sess = fake_session()
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(item_object(item_guid, entry=159))
        world.update_object(uo.UpdateBlock(update_type=uo.UPDATETYPE_VALUES, guid=0x1,
                                            fields={uf.UNIT_FIELD_FLAGS: ac.UNIT_FLAG_IN_COMBAT}))
        err = ac.UseItemAction().check(sess, world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("combat", err)

    def test_execute_sends_use_item_with_looked_up_spell(self):
        sess = fake_session()
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(item_object(item_guid, entry=159))
        world.items.items[159] = {"entry": 159, "found": True, "name": "Tough Jerky",
                                   "spells": [{"spell_id": 433, "trigger": ac.lootmod.ITEM_SPELLTRIGGER_ON_USE}]}

        result = ac.UseItemAction().execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.lootmod.CMSG_USE_ITEM)
        bag, slot, cast_count, spell_id = struct.unpack_from('<BBBI', payload, 0)
        item_guid_on_wire = struct.unpack_from('<Q', payload, 7)[0]
        self.assertEqual((bag, slot, spell_id), (255, 23, 433))
        self.assertEqual(item_guid_on_wire, item_guid)
        self.assertEqual(result.detail["spell_id"], 433)


class DestroyItemActionTest(unittest.TestCase):
    def test_requires_confirm(self):
        sess = fake_session()
        world = per.WorldState()
        err = ac.DestroyItemAction().check(sess, world, bag=255, slot=23, count=1, confirm=False)
        self.assertIsNotNone(err)
        self.assertIn("confirm", err)

    def test_execute_sends_destroyitem(self):
        sess = fake_session()
        world = per.WorldState()
        result = ac.DestroyItemAction().run(sess, world, bag=255, slot=23, count=2, confirm=True)
        self.assertTrue(result.ok)
        opcode, payload = sess._sent[0]
        self.assertEqual(opcode, ac.lootmod.CMSG_DESTROYITEM)
        self.assertEqual(payload, struct.pack('<BBI', 255, 23, 2))


WARRIOR_SWORD_TEMPLATE = {
    "entry": 1001, "found": True, "name": "Warrior Sword", "class_": 2, "subclass": 7,
    "inventory_type": 13, "item_level": 20, "allowable_class": -1,
    "stats": [{"type": 4, "value": 10}], "armor": 0, "damage": [{"min": 5.0, "max": 10.0, "type": 0}],
    "spells": [],
}

MAGE_STAFF_TEMPLATE = {
    "entry": 1002, "found": True, "name": "Mage Staff", "class_": 2, "subclass": 10,
    "inventory_type": 17, "item_level": 25, "allowable_class": -1,
    "stats": [{"type": 5, "value": 15}], "armor": 0, "damage": [{"min": 3.0, "max": 6.0, "type": 0}],
    "spells": [],
}

PLATE_CHEST_TEMPLATE = {
    "entry": 1003, "found": True, "name": "Plate Chest", "class_": 4, "subclass": 4,
    "inventory_type": 5, "item_level": 30, "allowable_class": -1,
    "stats": [{"type": 4, "value": 8}], "armor": 200, "damage": [], "spells": [],
}

CLOTH_ROBE_ONLY_MAGE_TEMPLATE = {
    "entry": 1004, "found": True, "name": "Mage Robe", "class_": 4, "subclass": 1,
    "inventory_type": 5, "item_level": 18, "allowable_class": 1 << (ac.item_compare.CLASS_MAGE - 1),
    "stats": [{"type": 5, "value": 6}], "armor": 40, "damage": [], "spells": [],
}


class CompareItemsActionTest(unittest.TestCase):
    def _world_with(self, slot_a, entry_a, template_a, slot_b, entry_b, template_b):
        world = per.WorldState()
        world.set_my_guid(0x1)
        guid_a = 0xF120000000000001
        guid_b = 0xF120000000000002
        world.update_object(self_player_with_slots(0x1, {slot_a: guid_a, slot_b: guid_b}))
        world.update_object(item_object(guid_a, entry=entry_a))
        world.update_object(item_object(guid_b, entry=entry_b))
        world.items.items[entry_a] = template_a
        world.items.items[entry_b] = template_b
        return world

    def test_check_fails_when_template_not_cached_yet(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        guid_a = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: guid_a}))
        world.update_object(item_object(guid_a, entry=1001))
        err = ac.CompareItemsAction().check(fake_session(), world, bag_a=255, slot_a=23,
                                             bag_b=255, slot_b=24)
        self.assertIsNotNone(err)
        self.assertIn("not queried yet", err)

    def test_check_fails_for_different_equip_slots(self):
        world = self._world_with(23, 1001, WARRIOR_SWORD_TEMPLATE, 24, 1003, PLATE_CHEST_TEMPLATE)
        err = ac.CompareItemsAction().check(fake_session(), world, bag_a=255, slot_a=23,
                                             bag_b=255, slot_b=24)
        self.assertIsNotNone(err)
        self.assertIn("different equip slots", err)

    def test_execute_picks_higher_primary_stat_for_class(self):
        # Same inventory_type (weapon slot 13/two-hand not required here, both use
        # inventory_type=13/17 in fixtures — re-key both to the same slot for this test).
        template_b = dict(MAGE_STAFF_TEMPLATE, inventory_type=WARRIOR_SWORD_TEMPLATE["inventory_type"])
        world = self._world_with(23, 1001, WARRIOR_SWORD_TEMPLATE, 24, 1002, template_b)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        result = ac.CompareItemsAction().execute(sess, world, bag_a=255, slot_a=23, bag_b=255, slot_b=24)
        self.assertTrue(result.ok)
        # Warrior's primary stat (Strength) only appears on item A -> A wins.
        self.assertEqual(result.detail["winner"], "a")
        self.assertIn("Warrior Sword", result.detail["reason"])

    def test_execute_flags_unusable_item_without_crashing(self):
        template_b = dict(PLATE_CHEST_TEMPLATE, inventory_type=WARRIOR_SWORD_TEMPLATE["inventory_type"])
        world = self._world_with(23, 1001, WARRIOR_SWORD_TEMPLATE, 24, 1003, template_b)
        sess = fake_session(class_=ac.item_compare.CLASS_MAGE)
        result = ac.CompareItemsAction().execute(sess, world, bag_a=255, slot_a=23, bag_b=255, slot_b=24)
        self.assertTrue(result.ok)  # comparing is always safe; unusability is just reported
        self.assertIsNotNone(result.detail["usability_error_b"])
        self.assertIn("proficiency", result.detail["usability_error_b"])


class EquipItemActionTest(unittest.TestCase):
    def _world_with_item(self, slot, entry, template):
        world = per.WorldState()
        world.set_my_guid(0x1)
        guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {slot: guid}))
        world.update_object(item_object(guid, entry=entry))
        if template is not None:
            world.items.items[entry] = template
        return world, guid

    def test_check_fails_for_empty_slot(self):
        world = per.WorldState()
        world.set_my_guid(0x1)
        world.update_object(self_player_with_slots(0x1, {}))
        err = ac.EquipItemAction().check(fake_session(), world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("no known item", err)

    def test_check_rejects_item_character_cannot_use(self):
        world, _ = self._world_with_item(23, 1004, CLOTH_ROBE_ONLY_MAGE_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        err = ac.EquipItemAction().check(sess, world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("not usable by class", err)

    def test_check_passes_when_class_matches(self):
        world, _ = self._world_with_item(23, 1004, CLOTH_ROBE_ONLY_MAGE_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_MAGE)
        self.assertIsNone(ac.EquipItemAction().check(sess, world, bag=255, slot=23))

    def test_check_allows_unqueried_item_through_to_the_server(self):
        # Template not cached yet -> can't pre-validate, so check() doesn't block it
        # (the server's own inventory_change_failure is the backstop).
        world, _ = self._world_with_item(23, 1001, None)
        self.assertIsNone(ac.EquipItemAction().check(fake_session(), world, bag=255, slot=23))

    def test_execute_sends_autoequip_and_succeeds_when_no_failure_arrives(self):
        world, guid = self._world_with_item(23, 1001, WARRIOR_SWORD_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        action = ac.EquipItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (ac.lootmod.CMSG_AUTOEQUIP_ITEM,
                                          ac.lootmod.build_autoequip_item(255, 23)))
        self.assertEqual(result.detail["item_guid"], guid)

    def test_execute_fails_on_inventory_change_failure(self):
        world, _ = self._world_with_item(23, 1001, WARRIOR_SWORD_TEMPLATE)
        sess = fake_session(class_=ac.item_compare.CLASS_WARRIOR)
        action = ac.EquipItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "inventory_change_failure", "result": 50,
                                         "reason_name": "inventory_full"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "inventory_full")


class OpenTradeActionTest(unittest.TestCase):
    def test_check_fails_for_unperceived_guid(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        self.assertIsNotNone(ac.OpenTradeAction().check(sess, world, guid=5))

    def test_check_fails_for_non_player_target(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(npc_object(5, 2.0, 0.0, 0.0))
        err = ac.OpenTradeAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("not a player", err)

    def test_check_fails_out_of_range(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 50.0, 0.0, 0.0, object_type="player"))
        err = ac.OpenTradeAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("move_towards", err)

    def test_check_fails_when_trade_already_pending(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))
        world.start_trade_request(5, initiated_by_me=True)
        err = ac.OpenTradeAction().check(sess, world, guid=5)
        self.assertIsNotNone(err)
        self.assertIn("already pending", err)

    def test_execute_starts_request_optimistically_and_sends_packet(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))
        result = ac.OpenTradeAction().execute(sess, world, guid=5)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_INITIATE_TRADE, tr.build_initiate_trade(5)))
        self.assertEqual(world.get_trade()["phase"], "requested")

    def test_execute_fails_on_rejection_event(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 0.0))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 2.0, 0.0, 0.0, object_type="player"))
        append_event_after(sess, 0.02, {"kind": "trade_cancelled", "reason": "target_to_far"})
        result = ac.OpenTradeAction().execute(sess, world, guid=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "target_to_far")


class AcceptTradeRequestActionTest(unittest.TestCase):
    def test_check_fails_without_pending_request(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.AcceptTradeRequestAction().check(fake_session(), world))

    def test_check_fails_when_initiated_by_me(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        err = ac.AcceptTradeRequestAction().check(fake_session(), world)
        self.assertIsNotNone(err)
        self.assertIn("wait for the other side", err)

    def test_execute_sends_begin_trade_and_succeeds_without_reply(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=False)
        sess = fake_session()
        action = ac.AcceptTradeRequestAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_BEGIN_TRADE, tr.build_begin_trade()))

    def test_execute_fails_on_trade_cancelled_event(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=False)
        sess = fake_session()
        action = ac.AcceptTradeRequestAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_cancelled", "reason": "busy"})
        result = action.execute(sess, world)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "busy")


class OfferItemActionTest(unittest.TestCase):
    def _world_with_open_trade_and_item(self, item_flags=0):
        world = per.WorldState()
        world.set_my_guid(0x1)
        item_guid = 0xF120000000000001
        world.update_object(self_player_with_slots(0x1, {23: item_guid}))
        world.update_object(uo.UpdateBlock(
            update_type=uo.UPDATETYPE_CREATE_OBJECT, guid=item_guid, object_type=uo.TYPEID_ITEM,
            movement={"update_flags": 0},
            fields={uf.OBJECT_FIELD_ENTRY: 6948, uf.ITEM_FIELD_STACK_COUNT: 1,
                    uf.ITEM_FIELD_FLAGS: item_flags},
        ))
        world.start_trade_request(5, initiated_by_me=True)
        world.apply_trade_status({"status": tr.TRADE_STATUS_OPEN_WINDOW, "status_name": "open_window"})
        return world, item_guid

    def test_check_fails_without_open_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23))

    def test_check_fails_for_unknown_item(self):
        world, _ = self._world_with_open_trade_and_item()
        err = ac.OfferItemAction().check(fake_session(), world, bag=255, slot=5)
        self.assertIsNotNone(err)
        self.assertIn("no known item", err)

    def test_check_fails_for_soulbound_item(self):
        world, _ = self._world_with_open_trade_and_item(item_flags=tr.ITEM_FIELD_FLAG_SOULBOUND)
        err = ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23)
        self.assertIsNotNone(err)
        self.assertIn("soulbound", err)

    def test_check_fails_for_out_of_range_trade_slot(self):
        world, _ = self._world_with_open_trade_and_item()
        err = ac.OfferItemAction().check(fake_session(), world, bag=255, slot=23, trade_slot=6)
        self.assertIsNotNone(err)

    def test_execute_auto_picks_first_free_slot_and_succeeds(self):
        world, item_guid = self._world_with_open_trade_and_item()
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "back_to_trade"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_SET_TRADE_ITEM, tr.build_set_trade_item(0, 255, 23)))
        self.assertEqual(world.get_trade()["my_items"][0]["entry"], 6948)

    def test_execute_skips_already_offered_slots(self):
        world, _ = self._world_with_open_trade_and_item()
        world.set_my_trade_item(0, {"entry": 1})
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "back_to_trade"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["trade_slot"], 1)

    def test_execute_fails_on_not_on_taplist(self):
        world, _ = self._world_with_open_trade_and_item()
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "not_on_taplist"})
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_on_taplist")
        self.assertNotIn(0, world.get_trade()["my_items"])

    def test_execute_times_out_as_failure(self):
        world, _ = self._world_with_open_trade_and_item()
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, bag=255, slot=23)
        self.assertFalse(result.ok)

    def test_execute_reoffering_same_item_in_same_slot_skips_the_round_trip(self):
        # Regression test (found live testing, 2026-09-17): TradeData::
        # SetItem early-returns with NO reply at all when this exact item
        # guid is already in this exact trade slot, so waiting for
        # back_to_trade would time out even on a legitimate no-op.
        world, item_guid = self._world_with_open_trade_and_item()
        world.set_my_trade_item(0, {"guid": item_guid, "entry": 6948})
        sess = fake_session()
        action = ac.OfferItemAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, bag=255, slot=23, trade_slot=0)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent, [])  # no packet sent at all


class OfferGoldActionTest(unittest.TestCase):
    def _world_with_open_trade(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        world.apply_trade_status({"status": tr.TRADE_STATUS_OPEN_WINDOW, "status_name": "open_window"})
        return world

    def test_check_fails_without_open_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.OfferGoldAction().check(fake_session(), world, amount=100))

    def test_check_fails_for_negative_amount(self):
        world = self._world_with_open_trade()
        self.assertIsNotNone(ac.OfferGoldAction().check(fake_session(), world, amount=-1))

    def test_check_fails_for_insufficient_gold(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 50
        err = ac.OfferGoldAction().check(sess, world, amount=100)
        self.assertIsNotNone(err)
        self.assertIn("not enough gold", err)

    def test_check_passes_with_enough_gold(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 500
        self.assertIsNone(ac.OfferGoldAction().check(sess, world, amount=100))

    def test_execute_succeeds_on_back_to_trade(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        sess.coinage = 500
        action = ac.OfferGoldAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "back_to_trade"})
        result = action.execute(sess, world, amount=100)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_SET_TRADE_GOLD, tr.build_set_trade_gold(100)))
        self.assertEqual(world.get_trade()["my_gold"], 100)

    def test_execute_fails_on_close_window(self):
        world = self._world_with_open_trade()
        sess = fake_session()
        action = ac.OfferGoldAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_status", "status_name": "close_window",
                                         "result_name": "not_enough_money"})
        result = action.execute(sess, world, amount=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_enough_money")
        self.assertEqual(world.get_trade()["my_gold"], 0)

    def test_execute_offering_the_same_amount_again_skips_the_round_trip(self):
        # Regression test (found live testing, 2026-09-17): TradeData::
        # SetMoney early-returns with NO reply at all when `amount` already
        # equals what's offered — my_gold starts at 0, so offer_gold(0)
        # right after opening a trade always timed out even on success.
        world = self._world_with_open_trade()
        sess = fake_session()
        action = ac.OfferGoldAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world, amount=0)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent, [])  # no packet sent at all


class AcceptTradeActionTest(unittest.TestCase):
    def _world_with_their_offer(self, gold=0, entries=()):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        world.apply_trade_status({"status": tr.TRADE_STATUS_OPEN_WINDOW, "status_name": "open_window"})
        items = {i: {"slot": i, "entry": e} for i, e in enumerate(entries)}
        world.apply_trade_status_extended({"is_trader_data": True, "money": gold, "items": items})
        return world

    def test_check_fails_without_open_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.AcceptTradeAction().check(fake_session(), world))

    def test_check_fails_when_gold_mismatch(self):
        world = self._world_with_their_offer(gold=100)
        err = ac.AcceptTradeAction().check(fake_session(), world, expected_their_gold=0)
        self.assertIsNotNone(err)
        self.assertIn("offer has changed", err)

    def test_check_fails_when_items_mismatch(self):
        world = self._world_with_their_offer(entries=(6948,))
        err = ac.AcceptTradeAction().check(fake_session(), world, expected_their_item_entries=[])
        self.assertIsNotNone(err)

    def test_check_passes_when_offer_matches(self):
        world = self._world_with_their_offer(gold=100, entries=(6948, 159))
        err = ac.AcceptTradeAction().check(fake_session(), world, expected_their_gold=100,
                                            expected_their_item_entries=[159, 6948])
        self.assertIsNone(err)  # order-independent

    def test_execute_returns_ok_waiting_when_no_reply(self):
        world = self._world_with_their_offer()
        sess = fake_session()
        action = ac.AcceptTradeAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_ACCEPT_TRADE, tr.build_accept_trade()))
        self.assertTrue(world.get_trade()["my_accepted"])

    def test_execute_returns_ok_on_trade_completed(self):
        world = self._world_with_their_offer()
        sess = fake_session()
        action = ac.AcceptTradeAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_completed", "summary": {}})
        result = action.execute(sess, world)
        self.assertTrue(result.ok)

    def test_execute_fails_on_trade_cancelled(self):
        world = self._world_with_their_offer()
        sess = fake_session()
        action = ac.AcceptTradeAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01
        append_event_after(sess, 0.02, {"kind": "trade_cancelled", "reason": "trade_canceled"})
        result = action.execute(sess, world)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "trade_canceled")


class CancelTradeActionTest(unittest.TestCase):
    def test_check_fails_without_active_trade(self):
        world = per.WorldState()
        self.assertIsNotNone(ac.CancelTradeAction().check(fake_session(), world))

    def test_execute_sends_cancel_and_reports_ok(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        sess = fake_session()
        action = ac.CancelTradeAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        result = action.execute(sess, world)
        self.assertTrue(result.ok)
        self.assertEqual(sess._sent[0], (tr.CMSG_CANCEL_TRADE, tr.build_cancel_trade()))

    def test_execute_reports_ok_and_clears_on_cancel_confirmation(self):
        world = per.WorldState()
        world.start_trade_request(5, initiated_by_me=True)
        sess = fake_session()
        action = ac.CancelTradeAction()
        action.confirm_timeout = 0.2
        action.confirm_interval = 0.01

        def cancel_after(delay):
            time.sleep(delay)
            world.apply_trade_status({"status": tr.TRADE_STATUS_TRADE_CANCELED,
                                       "status_name": "trade_canceled"})
            sess.events.append({"kind": "trade_cancelled", "reason": "trade_canceled",
                                 "t": time.monotonic()})
        t = threading.Thread(target=cancel_after, args=(0.02,))
        t.start()
        result = action.execute(sess, world)
        t.join()
        self.assertTrue(result.ok)
        self.assertIsNone(world.get_trade())


if __name__ == '__main__':
    unittest.main()
