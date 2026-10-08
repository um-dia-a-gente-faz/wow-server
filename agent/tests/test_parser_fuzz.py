"""Fuzz every packet handler on the ROUTER with truncated, bit-flipped and
extreme-count variants of fixture packets (seeded, stdlib only, #307).

A handler may raise (WoWSession._dispatch_guarded drops the packet) but only
a `ProtocolError`, must leave the session state unchanged when it does (#363),
and it must return fast and keep allocations bounded: a count
field of 2^32 driving a loop would hang or exhaust the recv thread, which the
guard cannot catch. Opcodes with no captured fixture get synthetic seeds."""

import logging
import os
import random
import time
import tempfile
import tracemalloc
import unittest
import uuid
from collections import deque
from unittest import mock

from agent import opcodes as op
from agent import update_object as uo
from agent import session as _session  # noqa: F401  (registers every handler)
from agent.items import ItemCache
from agent.names import NameCache
from agent.packets import ProtocolError
from agent.router import ROUTER
from agent.tests import builders as b
from agent.tests.builders import make_session
from agent.tests.test_perception_golden import build_world

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ITERATIONS = int(os.environ.get("FUZZ_ITERATIONS", "20"))  # per seed; nightly raises it
MAX_SECONDS = 1.0
MAX_BYTES = 32 * 1024 * 1024
# A malformed packet may raise ProtocolError (the router converts raw struct/index/value/zlib
# errors, #364); anything else is a bug. An opcode that cannot be fixed yet goes here with its
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
    }.get(opcode, [])


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
    # build_world() makes its own WorldState whose caches save to the real
    # ~/.cache/wow-agent/*.json on every response: keep those saves off disk, then
    # point the caches at the temp files make_session() uses.
    with mock.patch.object(NameCache, "save"), mock.patch.object(ItemCache, "save"):
        sess.world_state = build_world()
    sess.world_state.names.cache_path = os.path.join(tempfile.gettempdir(), f"wow-agent-test-names-{uuid.uuid4().hex}.json")
    sess.world_state.items.cache_path = os.path.join(tempfile.gettempdir(), f"wow-agent-test-items-{uuid.uuid4().hex}.json")
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

    def test_every_handler_survives_malformed_payloads(self):
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        for opcode in sorted(ROUTER._handlers):
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
                            ROUTER.dispatch(sess.ctx, opcode, payload)
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
