"""Tests for agent.actions.combat (split out of test_actions.py, issue #248).
"""

import struct
import unittest

from agent import actions as ac
from agent import movement as mv
from agent import perception as per
from agent import update_fields as uf
from agent import update_object as uo
from agent.tests.actions_helpers import fake_session, object_at, append_event_after


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

    def test_execute_faces_target_before_attackswing(self):
        sess = fake_session(player_position=(530, 0.0, 0.0, 0.0, 3.14))
        world = per.WorldState()
        world.set_my_map(530)
        world.update_object(object_at(5, 3.0, 0.0, 0.0))  # due east
        action = ac.AutoAttackAction()
        action.confirm_timeout = 0.05
        action.confirm_interval = 0.01
        action.execute(sess, world, guid=5)
        opcodes = [op for op, _ in sess._sent]
        self.assertIn(mv.MSG_MOVE_SET_FACING, opcodes)
        self.assertLess(opcodes.index(mv.MSG_MOVE_SET_FACING),
                        opcodes.index(ac.CMSG_ATTACKSWING))
        self.assertAlmostEqual(sess.player_position[4], 0.0, places=4)

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
