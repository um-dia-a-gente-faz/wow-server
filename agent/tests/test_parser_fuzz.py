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
import tracemalloc
import unittest
from collections import deque

from agent import opcodes as op
from agent import update_object as uo
from agent import session as _session  # noqa: F401  (registers every handler)
from agent.packets import ProtocolError
from agent.router import ROUTER
from agent.tests import builders as b
from agent.tests.builders import make_session

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


def _changed(before, after):
    """Top-level attributes that differ (a short message; assertEqual on two
    deep dicts spends minutes in difflib)."""
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


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

    def test_every_handler_survives_malformed_payloads(self):
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        for opcode in sorted(ROUTER._handlers):
            rng = random.Random(opcode)
            for s, seed in enumerate(seeds_for(opcode)):
                for i, payload in enumerate(variants(seed, rng)):
                    if i % 16 == 0:  # fresh state keeps the before/after snapshots small
                        sess = make_session()
                    with self.subTest(opcode=hex(opcode), seed=s, i=i, payload=payload[:40].hex(), n=len(payload)):
                        tracemalloc.reset_peak()
                        before = snapshot(sess)
                        t0 = time.monotonic()
                        try:
                            ROUTER.dispatch(sess.ctx, opcode, payload)
                        except ProtocolError:
                            # A handler that fails must not have changed state.
                            self.assertEqual(_changed(before, snapshot(sess)), [])
                        except Exception as e:
                            if opcode not in KNOWN_EXCEPTIONS:
                                raise AssertionError(f"{type(e).__name__} is not a ProtocolError: {e}") from e
                        self.assertLess(time.monotonic() - t0, MAX_SECONDS)
                        self.assertLess(tracemalloc.get_traced_memory()[1], MAX_BYTES)


if __name__ == '__main__':
    unittest.main()
