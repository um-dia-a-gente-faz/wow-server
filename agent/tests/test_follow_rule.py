"""#318: the follow rule. Follow is off by default and starts only from an explicit
decision (an accepted party invite, or an escort mission); the target must be a human or
the party leader, never the agent itself, never an agent that could itself be following."""

import unittest

from agent import actions as ac
from agent.reflexes import follow as fl
from agent.tests.test_follow_reflex import fast_session, object_at, set_health

ME, LEADER, MEMBER, HUMAN = 0x10, 0x20, 0x30, 0x40


def party(leader, members, offline=()):
    """The dict handlers/group.py stores (agent.group.parse_group_list): the receiver is not in members."""
    return {"guid": 1, "counter": 1, "raid": False, "leader_guid": leader,
            "members": [{"name": n, "guid": g, "online": g not in offline, "subgroup": 0,
                         "assistant": False, "roles": 0} for n, g in members]}


def make(group=None, agents=("Ana", "Bia", "Cai"), name="Ana", players=()):
    sess, world = fast_session(guid=ME)
    sess.player_name = name
    sess.group = group
    sess.agent_names = frozenset(a.lower() for a in agents)
    for guid, pname in players:
        world.update_object(object_at(guid, 1.0, 0.0, 0.0, object_type="player"))
        world.get_object(guid).name = pname
    return sess, world


def check_follow(sess, world, who):
    return ac.REGISTRY["follow"].check(sess, world, player_name=who)


class FollowTargetRuleTest(unittest.TestCase):
    def test_self_is_rejected_with_reason(self):
        sess, world = make(players=[(ME, "Ana")])
        self.assertIn("itself", check_follow(sess, world, "Ana"))

    def test_agent_chain_is_rejected_with_reason(self):
        # Ana would follow Bia, who may follow Cai: Bia is an agent and not Ana's leader.
        sess, world = make(group=party(LEADER, [("Dan", LEADER), ("Bia", MEMBER)]), players=[(MEMBER, "Bia")])
        err = check_follow(sess, world, "Bia")
        self.assertIn("agent", err)
        self.assertIn("not the party leader", err)

    def test_agent_follows_follower_is_rejected_with_reason(self):
        # Ana would follow Bia while Bia follows Ana.
        sess, world = make(players=[(MEMBER, "Bia")])
        err = check_follow(sess, world, "Bia")
        self.assertIn("agent", err)
        self.assertIn("not the party leader", err)

    def test_agent_name_match_ignores_case(self):
        sess, world = make(players=[(MEMBER, "bIA")])
        self.assertIsNotNone(check_follow(sess, world, "bIA"))

    def test_leader_agent_is_a_valid_target(self):
        sess, world = make(group=party(LEADER, [("Bia", LEADER)]), players=[(LEADER, "Bia")])
        self.assertIsNone(check_follow(sess, world, "Bia"))

    def test_human_is_a_valid_target(self):
        sess, world = make(players=[(HUMAN, "Rubens")])
        self.assertIsNone(check_follow(sess, world, "Rubens"))

    def test_party_leader_never_follows_a_member(self):
        sess, world = make(group=party(ME, [("Rubens", HUMAN)]), players=[(HUMAN, "Rubens")])
        self.assertIn("leads the party", check_follow(sess, world, "Rubens"))

    def test_execute_does_not_start_an_invalid_follow(self):
        sess, world = make(players=[(MEMBER, "Bia")])
        res = ac.REGISTRY["follow"].run(sess, world, player_name="Bia")
        self.assertFalse(res.ok)
        self.assertFalse(fl.get_follow_reflex(sess).enabled)


class OffByDefaultTest(unittest.TestCase):
    def test_nothing_starts_follow_without_a_decision(self):
        sess, world = make(group=party(LEADER, [("Bia", LEADER)]), players=[(LEADER, "Bia")])
        reflex = fl.get_follow_reflex(sess)
        for _ in range(3):
            reflex.tick(sess, world)
        self.assertFalse(reflex.enabled)

    def test_an_offered_invite_does_not_start_follow(self):
        sess, world = make(group=party(LEADER, [("Bia", LEADER)]), players=[(LEADER, "Bia")])
        sess.pending_invite = {"inviter_name": "Bia"}  # offered, not accepted
        fl.get_follow_reflex(sess).tick(sess, world)
        self.assertFalse(fl.get_follow_reflex(sess).enabled)

    def test_accepting_an_invite_from_a_valid_leader_starts_follow(self):
        sess, world = make(players=[(LEADER, "Rubens")])
        sess.pending_invite = {"inviter_name": "Rubens"}
        ac.accept_group(sess)
        reflex = fl.get_follow_reflex(sess)
        reflex.tick(sess, world)
        self.assertFalse(reflex.enabled)  # the server has not confirmed the party yet
        sess.group = party(LEADER, [("Rubens", LEADER)])
        reflex.tick(sess, world)
        self.assertTrue(reflex.enabled)
        self.assertEqual(reflex.leader_guid, LEADER)
        started = [e for e in sess.events if e["kind"] == "follow_started"]
        self.assertEqual(started[-1]["reason"], "party_invite")

    def test_accepting_an_invite_from_an_agent_leader_follows_it(self):
        sess, world = make(players=[(LEADER, "Bia")])
        sess.pending_invite = {"inviter_name": "Bia"}
        ac.accept_group(sess)
        sess.group = party(LEADER, [("Bia", LEADER)])
        fl.get_follow_reflex(sess).tick(sess, world)
        self.assertTrue(fl.get_follow_reflex(sess).enabled)

    def test_leader_that_is_not_the_inviter_is_not_followed(self):
        sess, world = make(players=[(LEADER, "Rubens"), (HUMAN, "Zed")])
        sess.pending_invite = {"inviter_name": "Rubens"}
        ac.accept_group(sess)
        sess.group = party(HUMAN, [("Zed", HUMAN), ("Rubens", LEADER)])
        fl.get_follow_reflex(sess).tick(sess, world)
        self.assertFalse(fl.get_follow_reflex(sess).enabled)

    def test_an_accepted_invite_is_consumed_once(self):
        sess, world = make(players=[(LEADER, "Rubens")])
        sess.pending_invite = {"inviter_name": "Rubens"}
        ac.accept_group(sess)
        sess.group = party(LEADER, [("Rubens", LEADER)])
        reflex = fl.get_follow_reflex(sess)
        reflex.tick(sess, world)
        reflex.stop(sess, world, reason="llm_requested")
        reflex.tick(sess, world)
        self.assertFalse(reflex.enabled)

    def test_escort_mission_hook_is_validated_like_any_start(self):
        sess, world = make(players=[(HUMAN, "Rubens"), (MEMBER, "Bia")])
        self.assertIsNone(fl.start_for_escort_mission(sess, world, HUMAN, "Rubens"))
        started = [e for e in sess.events if e["kind"] == "follow_started"]
        self.assertEqual(started[-1]["reason"], "escort_mission")
        fl.get_follow_reflex(sess).stop(sess, world)
        self.assertIsNotNone(fl.start_for_escort_mission(sess, world, MEMBER, "Bia"))
        self.assertFalse(fl.get_follow_reflex(sess).enabled)


class StopConditionsTest(unittest.TestCase):
    def started(self, extra=()):
        sess, world = make(group=party(LEADER, [("Rubens", LEADER), *extra]), players=[(LEADER, "Rubens")])
        self.assertIsNone(fl.begin_follow(sess, world, LEADER, "Rubens", reason="party_invite"))
        return sess, world, fl.get_follow_reflex(sess)

    def assert_stopped(self, sess, reflex, reason):
        self.assertFalse(reflex.enabled)
        stops = [e for e in sess.events if e["kind"] == "follow_stopped"]
        self.assertEqual(stops[-1]["reason"], reason)

    def test_agent_leaves_the_party(self):
        sess, world, reflex = self.started()
        sess.group = None
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "left_party")

    def test_target_leaves_the_party(self):
        sess, world, reflex = self.started(extra=[("Zed", HUMAN)])
        sess.group = party(HUMAN, [("Zed", HUMAN)])
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "target_left_party")

    def test_target_goes_offline(self):
        sess, world, reflex = self.started()
        sess.group = party(LEADER, [("Rubens", LEADER)], offline={LEADER})
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "target_offline")

    def test_agent_dies(self):
        sess, world, reflex = self.started()
        sess._was_alive = False
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "self_died")

    def test_target_dies(self):
        sess, world, reflex = self.started()
        set_health(world, LEADER, 0)
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "target_died")

    def test_agent_becomes_party_leader(self):
        sess, world, reflex = self.started()
        sess.group = party(ME, [("Rubens", LEADER)])
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "target_invalid")

    def test_agent_leader_is_dropped_once_it_is_no_longer_leader(self):
        sess, world = make(group=party(LEADER, [("Bia", LEADER), ("Zed", HUMAN)]), players=[(LEADER, "Bia")])
        fl.begin_follow(sess, world, LEADER, "Bia", reason="party_invite")
        sess.group = party(HUMAN, [("Bia", LEADER), ("Zed", HUMAN)])
        reflex = fl.get_follow_reflex(sess)
        reflex.tick(sess, world)
        self.assert_stopped(sess, reflex, "target_invalid")

    def test_brain_stop_records_why(self):
        sess, world, reflex = self.started()
        ac.REGISTRY["stop_following"].run(sess, world)
        self.assert_stopped(sess, reflex, "llm_requested")


if __name__ == "__main__":
    unittest.main()
