"""#318 scenario: two agents in one party run N cycles and no follow cycle forms.

Both agents are real sessions (real handlers, real accept/invite actions, real follow
reflex). A tiny in-process realm stands in for the server and routes their packets: the
world_mock (tools/world_mock) serves one scripted character per connection and has no
routing between connections, so two agents cannot share a party there.

Each cycle the worst-case brain is played: both agents try to follow the other, then both
reflexes tick. Allowed outcomes per cycle: neither follows, or exactly one does."""

import struct
import time
import unittest

from agent import actions as ac
from agent import opcodes as op
from agent.handlers import chat as chat_handlers
from agent.handlers import group as group_handlers
from agent.reflexes import follow as fl
from agent.router import Context
from agent.tests.test_follow_reflex import fast_session, object_at
from agent.tests.test_group import group_list, member

CYCLES = 25
ANA, BIA = 0x1001, 0x1002


class Realm:
    """Routes CMSG_GROUP_INVITE / CMSG_GROUP_ACCEPT between two sessions like
    HandleGroupInviteOpcode / HandleGroupAcceptOpcode: the inviter leads, the
    group list goes to both members."""

    def __init__(self):
        self.agents = {}      # guid -> (session, world, name)
        self.leader = None
        self.members = []
        self.invites = {}     # invitee guid -> inviter guid

    def add(self, guid, name):
        sess, world = fast_session(guid=guid)
        sess.player_name = name
        sess.agent_names = frozenset({"ana", "bia"})
        ctx = Context(sess, None)
        sess._send_packet = lambda opcode, payload=b"", g=guid: self.on_packet(g, opcode, payload)
        self.agents[guid] = (sess, world, name, ctx)
        return sess, world

    def on_packet(self, sender, opcode, payload):
        if opcode == op.CMSG_GROUP_INVITE:
            target_name = payload.split(b"\0")[0].decode()
            for guid, (_s, _w, name, ctx) in self.agents.items():
                if name == target_name and guid not in self.invites and not self.members:
                    self.invites[guid] = sender
                    sender_name = self.agents[sender][2]
                    chat_handlers.handle_group_invite(ctx, b"\x01" + sender_name.encode() + b"\0" + struct.pack("<I", 0))
        elif opcode == op.CMSG_GROUP_ACCEPT and sender in self.invites:
            self.leader = self.invites.pop(sender)
            self.members = [self.leader, sender]
            for guid in self.members:
                self.send_list(guid)

    def send_list(self, to):
        others = [(self.agents[g][2], g) for g in self.members if g != to]
        payload = group_list([member(n, g) for n, g in others], self.leader)
        group_handlers.handle_group_list(self.agents[to][3], payload)


def see_each_other(realm):
    for guid, (_s, world, _n, _c) in realm.agents.items():
        for other, (_s2, _w2, name, _c2) in realm.agents.items():
            if other != guid:
                world.update_object(object_at(other, 1.0, 0.0, 0.0, object_type="player"))
                world.get_object(other).name = name


def play_cycles(realm, a_guid, b_guid):
    """N cycles of the worst case: each agent tries to follow the other, then both reflexes tick."""
    states = []
    for _ in range(CYCLES):
        for me, other in ((a_guid, b_guid), (b_guid, a_guid)):
            sess, world, _name, _ctx = realm.agents[me]
            other_name = realm.agents[other][2]
            ac.REGISTRY["follow"].run(sess, world, player_name=other_name)
        for me in (a_guid, b_guid):
            sess, world, _name, _ctx = realm.agents[me]
            fl.get_follow_reflex(sess).tick(sess, world)
        states.append(tuple(fl.get_follow_reflex(realm.agents[g][0]).enabled for g in (a_guid, b_guid)))
    return states


class TwoAgentsInOnePartyTest(unittest.TestCase):
    def setUp(self):
        self.realm = Realm()
        self.ana, self.ana_world = self.realm.add(ANA, "Ana")
        self.bia, self.bia_world = self.realm.add(BIA, "Bia")
        see_each_other(self.realm)

    def test_party_by_invite_and_accept_then_no_cycle(self):
        ac.invite_to_group(self.ana, "Bia")
        self.assertEqual(self.bia.pending_invite, {"inviter_name": "Ana"})
        ac.REGISTRY["accept_group"].run(self.bia, self.bia_world)
        self.assertEqual(self.realm.leader, ANA)
        states = play_cycles(self.realm, ANA, BIA)
        self.assertTrue(all(sum(s) <= 1 for s in states), states)
        self.assertNotIn((True, True), states)
        # the leader never follows its member; the member follows the leader
        self.assertEqual(states[-1], (False, True))

    def test_accepting_the_invite_starts_the_member_following_without_any_follow_action(self):
        ac.invite_to_group(self.ana, "Bia")
        ac.REGISTRY["accept_group"].run(self.bia, self.bia_world)
        fl.get_follow_reflex(self.bia).tick(self.bia, self.bia_world)
        fl.get_follow_reflex(self.ana).tick(self.ana, self.ana_world)
        self.assertTrue(fl.get_follow_reflex(self.bia).enabled)
        self.assertFalse(fl.get_follow_reflex(self.ana).enabled)

    def test_two_agents_outside_any_party_do_not_form_a_cycle(self):
        states = play_cycles(self.realm, ANA, BIA)
        self.assertEqual(set(states), {(False, False)})

    def test_a_declined_invite_leaves_both_idle(self):
        ac.invite_to_group(self.ana, "Bia")
        ac.REGISTRY["decline_group"].run(self.bia, self.bia_world)
        for _ in range(CYCLES):
            fl.get_follow_reflex(self.bia).tick(self.bia, self.bia_world)
        self.assertFalse(fl.get_follow_reflex(self.bia).enabled)


HUMAN = 0x2001


def group_view(leader, name):
    return {"guid": 1, "counter": 1, "raid": False, "leader_guid": leader, "loot_method": None,
            "members": [{"name": name, "guid": leader, "online": True, "subgroup": 0,
                         "assistant": False, "roles": 0}]}


class PendingAcceptTest(unittest.TestCase):
    def test_a_settled_accept_does_not_restart_follow_after_stop(self):
        sess, world = fast_session(guid=ANA)
        sess.player_name = "Ana"
        world.update_object(object_at(HUMAN, 1.0, 0.0, 0.0, object_type="player"))
        world.get_object(HUMAN).name = "Rubens"
        reflex = fl.get_follow_reflex(sess)
        self.assertTrue(ac.REGISTRY["follow"].run(sess, world, player_name="Rubens").ok)
        sess.pending_invite = {"inviter_name": "Rubens"}
        ac.accept_group(sess)
        sess.group = group_view(HUMAN, "Rubens")
        reflex.tick(sess, world)
        self.assertTrue(reflex.enabled)
        ac.REGISTRY["stop_following"].run(sess, world)
        reflex.tick(sess, world)
        self.assertFalse(reflex.enabled)

    def test_an_accept_with_no_party_list_is_dropped_after_the_window(self):
        sess, world = fast_session(guid=ANA)
        sess.pending_invite = {"inviter_name": "Rubens"}
        ac.accept_group(sess)
        reflex = fl.get_follow_reflex(sess)
        reflex.tick(sess, world)
        self.assertIsNotNone(sess.follow_on_accept)  # still inside the window
        sess.follow_on_accept = ("Rubens", time.monotonic() - 1)
        reflex.tick(sess, world)
        self.assertIsNone(sess.follow_on_accept)

    def test_an_expired_accept_is_dropped_even_when_a_group_exists(self):
        sess, world = fast_session(guid=ANA)
        sess.follow_on_accept = ("Rubens", time.monotonic() - 1)
        sess.group = group_view(HUMAN, "Rubens")
        fl.get_follow_reflex(sess).tick(sess, world)
        self.assertIsNone(sess.follow_on_accept)
        self.assertEqual([e for e in sess.events if e["kind"] == "follow_started"], [])


if __name__ == "__main__":
    unittest.main()
