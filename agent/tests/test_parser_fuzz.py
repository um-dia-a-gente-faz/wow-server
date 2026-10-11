"""Fuzz every packet handler on the session router with truncated, bit-flipped and
extreme-count variants of fixture packets (seeded, stdlib only, #307).

A handler may raise (WoWSession._dispatch_guarded drops the packet) but only
a `ProtocolError`, must leave the session state unchanged when it does (#363),
and it must return fast and keep allocations bounded: a count
field of 2^32 driving a loop would hang or exhaust the recv thread, which the
guard cannot catch. Opcodes with no captured fixture get synthetic seeds."""

import logging
import os
import random
import struct
import time
import tempfile
import tracemalloc
import unittest
from collections import deque
from unittest import mock

from agent import opcodes as op
from agent import update_object as uo
from agent.session import build_session_router
from agent.packets import ProtocolError
from agent.tests import builders as b
from agent.tests.builders import make_session
from agent.tests.test_perception_golden import build_world
# Test modules imported as namespaces, so their TestCase classes are not collected twice.
from agent import loot as lo
from agent.handlers.chat import CHAT_MSG_SAY
from agent.handlers.world import MSG_MOVE_OPCODES
from agent.tests import test_channels as tc
from agent.tests import test_loot as tl
from agent.tests import test_names as tn
from agent.tests import test_npc as tnpc
from agent.tests import test_quests as tq
from agent.tests import test_session as tse
from agent.tests import test_spells as tsp

_router = build_session_router()
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ITERATIONS = int(os.environ.get("FUZZ_ITERATIONS", "20"))  # per seed; nightly raises it
MAX_SECONDS = 1.0
MAX_BYTES = 32 * 1024 * 1024
# A malformed packet may raise ProtocolError (parsers read through wire.Reader, #517; the router
# no longer converts raw errors); anything else is a bug. An opcode that cannot be fixed yet goes here with its
# issue number, so the list can only shrink.
KNOWN_EXCEPTIONS: dict[int, str] = {}
EXTREME = (b'\xff\xff\xff\xff', b'\x00\x00\x00\x80', b'\xff\xff\x00\x00', b'\x00\x00\x00\x00')

# fixture dir/file prefix -> router opcodes it seeds
FIXTURE_OPCODES = {
    "update_object/": (op.SMSG_UPDATE_OBJECT,),
    "quests/quest_query": (op.SMSG_QUEST_QUERY_RESPONSE,),
    "quests/questupdate": (op.SMSG_QUESTUPDATE_ADD_KILL,),
    "trade/trade_status_extended": (op.SMSG_TRADE_STATUS_EXTENDED,),
    "trade/trade_status_": (op.SMSG_TRADE_STATUS,),
    "mail/send_mail": (0x239,),
    "mail/mail_list": (0x23B,),
    "chat/": (op.SMSG_MESSAGECHAT,),
    "monster_move/": (op.SMSG_MONSTER_MOVE,),
}


def _load(path):
    with open(path, "rb") as f:
        data = f.read()
    return bytes.fromhex(data.decode().split("#", 1)[0]) if path.endswith(".hex") else data


def seeds_for(opcode):
    out = []
    for prefix, ops in FIXTURE_OPCODES.items():
        if opcode not in ops:
            continue
        d, _, stem = prefix.partition("/")
        for name in sorted(os.listdir(os.path.join(FIXTURES, d))):
            if name.endswith((".bin", ".hex")) and name.startswith(stem):
                if prefix == "trade/trade_status_" and "extended" in name:
                    continue
                out.append(_load(os.path.join(FIXTURES, d, name)))
    # Synthetic seeds so every opcode is exercised, with and without a body.
    rng = random.Random(opcode)
    return out + builder_seeds(opcode) + [b'', bytes(16), bytes(rng.randrange(256) for _ in range(48))]


def builder_seeds(opcode):
    """Valid packets from tests/builders.py: the mutations start from the happy path."""
    blk = b.object_block(uo.UPDATETYPE_CREATE_OBJECT, 0x42)
    return {
        op.SMSG_UPDATE_OBJECT: [b.update_object(blk), b.update_object(blk, b.values_block(0x42))],
        op.SMSG_COMPRESSED_UPDATE_OBJECT: [b.compressed(b.update_object(blk))],
        op.SMSG_QUESTGIVER_OFFER_REWARD: [b.offer_reward_payload(1, 2, "t", "x", [(3, 1, 4)], [(5, 1, 6)])],
        op.SMSG_QUESTGIVER_QUEST_COMPLETE: [b.quest_complete_payload(2, 10, 20)],
        **valid_payloads(),
    }.get(opcode, [])


def valid_payloads() -> dict:
    """#517 review: one or more valid payloads for each opcode the fixtures and builders above do
    not cover. Without one, a fuzzed variant never parses, so the code after a successful parse is
    never fuzzed. Each layout is copied from the named existing test (namespace imports above)."""
    guid = 0xF130000000001234
    gossip = (struct.pack('<Q', guid) + struct.pack('<ii', 42, 999) + struct.pack('<I', 1)
              + tnpc.gossip_option_bytes(text="Train me") + struct.pack('<I', 0))   # test_npc ParseGossipMessageTest
    loot = (struct.pack('<Q', guid) + bytes([lo.LOOT_CORPSE]) + struct.pack('<IB', 55, 1)   # test_loot ParseLootResponseTest
            + struct.pack('<BIIIii', 0, 2589, 1, 12345, 0, -1) + bytes([0]))
    initial_spells = (bytes([1]) + struct.pack('<H', 2) + struct.pack('<IH', 635, 0)   # test_spells InitialSpellsTest
                      + struct.pack('<IH', 21084, 1) + struct.pack('<H', 1)
                      + struct.pack('<IHHii', 20271, 0, 0, 1500, 0))
    quest_list = (struct.pack('<Q', 0x1234) + tq.cstring("Greetings!")   # test_quests ParseQuestgiverQuestListTest
                  + struct.pack('<II', 0, 1) + bytes([1])   # one quest (the test has two)
                  + struct.pack('<IIiI', 100, 2, 10, 8) + bytes([0]) + tq.cstring("Quest A"))
    quest_details = (struct.pack('<QQI', 0x1234, 0, 8325)   # test_quests ParseQuestgiverQuestDetailsTest
                     + tq.cstring("Reclaiming Sunstrider Isle") + tq.cstring("Details here.")
                     + tq.cstring("Kill 8 wyrms.") + bytes([1]) + struct.pack('<II', 0x80088, 0) + bytes([0])
                     + tq.reward_list([(20997, 1, 111), (20998, 1, 222)]) + tq.reward_list([])
                     + struct.pack('<III', 30, 45, 0) + struct.pack('<f', 0.0)
                     + struct.pack('<IiIIII', 0, 0, 0, 0, 0, 1) + struct.pack('<5I', 911, 0, 0, 0, 0)
                     + struct.pack('<5i', 5, 0, 0, 0, 0) + struct.pack('<5i', 0, 0, 0, 0, 0)
                     + struct.pack('<i', 2) + struct.pack('<IIII', 1, 0, 0, 0))
    request_items = (struct.pack('<Qi', 0x1234, 100)   # test_quests ParseQuestgiverRequestItemsTest
                     + tq.cstring("Quest A") + tq.cstring("Bring me items.")
                     + struct.pack('<iiiIii', 0, 1, 0, 0, 0, 0) + struct.pack('<I', 1)
                     + struct.pack('<iiI', 2589, 5, 1234) + struct.pack('<IIII', 0, 4, 8, 16))
    trainer = (struct.pack('<Q', 0xF130000000009999) + struct.pack('<ii', 0, 1)   # test_npc ParseTrainerListTest
               + tnpc.trainer_spell_bytes(spell_id=587, req_level=10) + tq.cstring("Welcome, mage."))
    levelup = struct.pack('<II', 5, 20) + struct.pack('<7I', *range(7)) + struct.pack('<5I', *range(5))   # test_spells LevelUpInfoTest
    gm_chat = tse.messagechat_payload(CHAT_MSG_SAY, "hi", sender_guid=1, sender_name="Sender")   # test_session GM chat
    return {
        op.SMSG_ITEM_QUERY_SINGLE_RESPONSE: [tl.ParseItemQueryResponseTest._build(None)],   # test_loot _build
        op.SMSG_GAMEOBJECT_QUERY_RESPONSE: [struct.pack('<I', 181646) + tn.gameobject_response_body(name="Ship")],
        op.SMSG_CREATURE_QUERY_RESPONSE: [struct.pack('<I', 17213) + tn.creature_response_body(name="Broom")],
        op.SMSG_CHANNEL_NOTIFY: [tc.you_joined("General", 1)],   # test_channels
        op.SMSG_INITIAL_SPELLS: [initial_spells],
        op.SMSG_ATTACKERSTATEUPDATE: [tsp.attacker_state_update_body()],   # test_spells
        op.SMSG_LOOT_RESPONSE: [loot],
        op.SMSG_GOSSIP_MESSAGE: [gossip],
        op.SMSG_QUESTGIVER_QUEST_LIST: [quest_list],
        op.SMSG_QUESTGIVER_QUEST_DETAILS: [quest_details],
        op.SMSG_QUESTGIVER_REQUEST_ITEMS: [request_items],
        op.SMSG_TRAINER_LIST: [trainer],
        op.SMSG_LEVELUP_INFO: [levelup],
        op.SMSG_GM_MESSAGECHAT: [gm_chat],
        **{mv: [tse.heartbeat_payload(guid)] for mv in MSG_MOVE_OPCODES},   # MOVE: packed guid + 30-byte MovementInfo
    }


_SCALARS = {int, float, str, bytes, bool, type(None)}
_SKIP = {"sock", "_lock", "ctx", "_recv_thread", "_error_throttle", "dropped_packets"}


def snapshot(obj, _seen=None):
    """A comparable deep copy of the session's game state (attributes, slots,
    containers), so a handler that mutated state before failing shows as a diff."""
    seen = _seen if _seen is not None else set()
    if type(obj) in _SCALARS:
        return obj
    if id(obj) in seen:
        return "<cycle>"
    seen.add(id(obj))
    if isinstance(obj, dict):
        return {k if isinstance(k, (str, int)) else repr(k): snapshot(v, seen) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, deque)):
        return [snapshot(v, seen) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(repr(v) for v in obj)
    if not type(obj).__module__.startswith("agent"):
        return repr(obj)  # functions, classes, modules, locks: not game state
    attrs = dict(getattr(obj, "__dict__", {}))
    for cls in type(obj).__mro__:
        for name in getattr(cls, "__slots__", ()):
            if hasattr(obj, name):
                attrs[name] = getattr(obj, name)
    return {k: snapshot(v, seen) for k, v in attrs.items() if k not in _SKIP}


_MISSING = object()  # a key absent on one side differs from a None value


def _changed(before, after, path="session"):
    """Dotted paths of the leaves that differ (a short message; assertEqual on
    two deep dicts spends minutes in difflib)."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [p for k in sorted(before.keys() | after.keys(), key=str)
                for p in _changed(before.get(k, _MISSING), after.get(k, _MISSING), f"{path}.{k}")]
    return [path]


def populated_session():
    """A session with state to lose: the perception golden world (self, items,
    units, quest log, trade, mailbox, channel) plus open loot, a pending invite,
    a group and spells. A write that depends on existing state (#418) is only
    reachable here."""
    sess = make_session()
    sess.world_state = build_world()
    sess.loot = {"coins": 5, "items": []}
    sess.pending_invite = {"inviter_name": "Bob"}
    sess.group = {"leader_guid": 0x12, "raid": False, "loot_method": 1, "members": []}
    sess.spellbook.add(133)
    return sess


def variants(seed, rng):
    n = len(seed)
    for cut in sorted(set(range(min(n, 48)))
                      | set(rng.randrange(n) for _ in range(ITERATIONS) if n)):
        yield seed[:cut]                                   # truncated
    for _ in range(ITERATIONS):
        b = bytearray(seed)
        for _ in range(rng.randint(1, 4)):                 # bit flips
            if b:
                b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
        yield bytes(b)
        if len(seed) >= 4:                                 # extreme count/length field
            b = bytearray(seed)
            at = rng.choice((0, 0, rng.randrange(len(b) - 3)))
            b[at:at + 4] = rng.choice(EXTREME)
            yield bytes(b)


class ParserFuzzTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)  # handlers warn on every malformed packet
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_snapshot_sees_state_changes(self):
        sess = make_session()
        for mutate in (lambda: setattr(sess, "level", 9),
                       lambda: sess.spellbook.add(1),
                       lambda: sess.events.append("x"),
                       lambda: setattr(sess.world_state, "player_guid", 7),
                       lambda: sess.world_state.names.__dict__.update(extra=1)):
            before = snapshot(sess)
            mutate()
            self.assertNotEqual(snapshot(sess), before)

    def test_changed_names_the_path_below_world_state(self):
        sess = populated_session()
        before = snapshot(sess)
        sess.world_state.player_guid = 99
        sess.loot["coins"] = 0
        self.assertEqual(_changed(before, snapshot(sess)), ["session.loot.coins", "session.world_state.player_guid"])
        sess.world_state.names.players[9] = None  # a new key holding None is a change
        self.assertIn("session.world_state.names.players.9", _changed(before, snapshot(sess)))

    def test_populated_session_never_writes_outside_a_temp_dir(self):
        with mock.patch("os.replace") as replace, mock.patch("builtins.open", mock.mock_open()) as opened:
            sess = populated_session()
            for cache in (sess.world_state.names, sess.world_state.items):
                cache.save()
        paths = [c.args[0] for c in opened.call_args_list if c.args and "w" in (c.args[1:2] or ("",))[0]]
        paths += [c.args[1] for c in replace.call_args_list]
        self.assertTrue(paths)
        self.assertTrue(all(p.startswith(tempfile.gettempdir()) for p in paths), paths)

    def test_every_registered_opcode_has_a_seed_that_parses(self):
        """#517 review: the fuzz walks truncations and flips of each seed, but an opcode whose
        seeds all fail never reaches the code after a successful parse. Each registered opcode
        needs one non-empty seed that dispatches on a fresh session without raising."""
        names = {v: k for k, v in vars(op).items() if isinstance(v, int) and k.split("_")[0] in ("SMSG", "MSG", "CMSG")}
        uncovered = []
        for opcode in sorted(_router._handlers):
            for seed in seeds_for(opcode):
                if not seed:
                    continue
                try:
                    _router.dispatch(make_session().ctx, opcode, seed)
                except Exception:
                    continue
                break
            else:
                uncovered.append(f"{opcode:#05x} {names.get(opcode, '?')}")
        self.assertEqual(uncovered, [], f"{len(uncovered)} registered opcodes with no non-empty seed "
                                        "that parses: " + ", ".join(uncovered))

    def test_every_handler_survives_malformed_payloads(self):
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        for opcode in sorted(_router._handlers):
            rng = random.Random(opcode)
            for s, seed in enumerate(seeds_for(opcode)):
                for i, payload in enumerate(variants(seed, rng)):
                    if i % 16 == 0:  # fresh state keeps the before/after snapshots small
                        # Every 4th batch is populated (both kinds reached); default run ~6 s (5.9-6.3 s over 10 runs, was 5.3-5.4 s on empty sessions only).
                        sess = populated_session() if (i // 16) % 4 == 1 else make_session()
                        before = snapshot(sess)
                    with self.subTest(opcode=hex(opcode), seed=s, i=i, payload=payload[:40].hex(), n=len(payload)):
                        tracemalloc.reset_peak()
                        t0 = time.monotonic()
                        ok = False
                        try:
                            _router.dispatch(sess.ctx, opcode, payload)
                            ok = True
                        except ProtocolError:
                            # A handler that fails must not have changed state. (One snapshot
                            # per dispatch: `before` is still valid after a failure.)
                            after = snapshot(sess)
                            changed, before = _changed(before, after), after  # one offender, one failure
                            self.assertEqual(changed, [])
                        except Exception as e:
                            if opcode not in KNOWN_EXCEPTIONS:
                                raise AssertionError(f"{type(e).__name__} is not a ProtocolError: {e}") from e
                        self.assertLess(time.monotonic() - t0, MAX_SECONDS)
                        if ok:
                            before = snapshot(sess)  # a normal return may change state: new baseline
                        self.assertLess(tracemalloc.get_traced_memory()[1], MAX_BYTES)


if __name__ == '__main__':
    unittest.main()
