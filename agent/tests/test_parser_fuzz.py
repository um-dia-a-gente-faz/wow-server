"""Fuzz every packet handler on the ROUTER with truncated, bit-flipped and
extreme-count variants of fixture packets (seeded, stdlib only, #307).

A handler may raise (WoWSession._dispatch_guarded drops the packet) but only
a parse error, and it must return fast and keep allocations bounded: a count
field of 2^32 driving a loop would hang or exhaust the recv thread, which the
guard cannot catch. Opcodes with no captured fixture get synthetic seeds."""

import logging
import os
import random
import struct
import time
import tracemalloc
import unittest
import zlib

from agent import opcodes as op
from agent import session as _session  # noqa: F401  (registers every handler)
from agent.perception import PerceptionParseError
from agent.router import ROUTER
from agent.tests.builders import make_session

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ITERATIONS = int(os.environ.get("FUZZ_ITERATIONS", "20"))  # per seed; nightly raises it
MAX_SECONDS = 1.0
MAX_BYTES = 32 * 1024 * 1024
# What a malformed packet may legitimately raise; anything else is a bug.
PARSE_ERRORS = (struct.error, IndexError, ValueError, UnicodeError, KeyError, zlib.error,
                PerceptionParseError)
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
    return out + [b'', bytes(16), bytes(rng.randrange(256) for _ in range(48))]


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

    def test_every_handler_survives_malformed_payloads(self):
        sess = make_session()
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        for opcode in sorted(ROUTER._handlers):
            rng = random.Random(opcode)
            for seed in seeds_for(opcode):
                for payload in variants(seed, rng):
                    with self.subTest(opcode=hex(opcode), payload=payload[:40].hex(), n=len(payload)):
                        tracemalloc.reset_peak()
                        t0 = time.monotonic()
                        try:
                            ROUTER.dispatch(sess.ctx, opcode, payload)
                        except PARSE_ERRORS:
                            pass
                        self.assertLess(time.monotonic() - t0, MAX_SECONDS)
                        self.assertLess(tracemalloc.get_traced_memory()[1], MAX_BYTES)


if __name__ == '__main__':
    unittest.main()
