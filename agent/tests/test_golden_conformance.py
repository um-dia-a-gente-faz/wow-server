"""Golden conformance (gh-349): every real capture under agent/tests/fixtures/<kind>/ is decoded by
the agent parser for its kind and checked against its sidecar, <capture>.expected.json.

The sidecar values were written from the TrinityCore 3.3.5 writer named in its "source" field and
cross-checked against the directory README, never taken from the parser. A capture without a
sidecar, a sidecar without a capture, or a capture directory without a decoder below fails the
suite. Parser and sidecar disagreeing is a finding, not a reason to edit the sidecar.

Run from the repo root: python3 -m unittest discover -t . -s agent/tests
"""

import json
import pathlib
import tempfile
import unittest
from collections.abc import Callable
from types import SimpleNamespace

from agent import session as se
from agent import mail as mail_mod
from agent import quests as qu
from agent import trade as tr
from agent import update_fields as uf
from agent import update_object as uo
from agent.handlers import chat as hchat
from agent.tests.builders import make_session, server_packet

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CAPTURE_SUFFIXES = (".bin", ".hex")
SIDECAR_SUFFIX = ".expected.json"


# ── Decoders: one per capture directory, each calling the agent parser for that kind ──────────

def _update_block(block: uo.UpdateBlock) -> dict:
    out: dict = {"update_type": block.update_type}
    if block.guids:
        out["guids"] = list(block.guids)
    if block.update_type not in (uo.UPDATETYPE_OUT_OF_RANGE_OBJECTS, uo.UPDATETYPE_NEAR_OBJECTS):
        out["guid"] = block.guid
    if block.object_type is not None:
        out["object_type"] = block.object_type
    movement = block.movement or {}
    for key in ("x", "y", "z", "o"):
        if key in movement:
            out[key] = movement[key]
    if "spline" in movement:
        out["spline"] = movement["spline"]
    if block.fields is not None:
        out["values"] = block.fields
        if block.object_type is not None:
            named = uf.decode_fields(block.object_type, block.fields)
            for key in ("entry", "health", "max_health", "level"):
                if key in named:
                    out[key] = named[key]
    return out


def decode_update_object(payload: bytes, opcode: str) -> dict:
    blocks = uo.parse_update_object(payload)
    return {"block_count": len(blocks), "blocks": [_update_block(b) for b in blocks]}


def decode_monster_move(payload: bytes, opcode: str) -> dict:
    return uo.parse_monster_move(payload)


def decode_quests(payload: bytes, opcode: str) -> dict:
    if opcode == "SMSG_QUEST_QUERY_RESPONSE":
        return qu.parse_quest_query_response(payload)
    if opcode == "SMSG_QUESTUPDATE_ADD_KILL":
        return qu.parse_questupdate_add_kill(payload)
    raise ValueError(f"quests decoder has no parser for {opcode}")


def decode_trade(payload: bytes, opcode: str) -> dict:
    if opcode == "SMSG_TRADE_STATUS":
        return tr.parse_trade_status(payload)
    if opcode == "SMSG_TRADE_STATUS_EXTENDED":
        info = tr.parse_trade_status_extended(payload)
        info["item_slots"] = sorted(info["items"])
        return info
    raise ValueError(f"trade decoder has no parser for {opcode}")


def decode_mail(payload: bytes, opcode: str) -> dict:
    if opcode == "SMSG_SEND_MAIL_RESULT":
        return mail_mod.parse_send_mail_result(payload)
    if opcode == "SMSG_MAIL_LIST_RESULT":
        return mail_mod.parse_mail_list_result(payload)
    raise ValueError(f"mail decoder has no parser for {opcode}")


def decode_char_enum(payload: bytes, opcode: str) -> list:
    return make_session(server_packet(se.SMSG_CHAR_ENUM, payload)).enum_characters()


def decode_chat(payload: bytes, opcode: str) -> dict:
    ctx = SimpleNamespace(state=SimpleNamespace(chat_inbox=[], chat_relay=None, world_state=None))
    hchat.handle_messagechat(ctx, hchat.SMSG_MESSAGECHAT, payload)
    return ctx.state.chat_inbox[-1]


DECODERS: dict[str, Callable[[bytes, str], object]] = {
    "update_object": decode_update_object,
    "monster_move": decode_monster_move,
    "quests": decode_quests,
    "trade": decode_trade,
    "mail": decode_mail,
    "char_enum": decode_char_enum,
    "chat": decode_chat,
}


# ── Discovery ────────────────────────────────────────────────────────────────────────────────

def captures(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in CAPTURE_SUFFIXES)


def sidecar_for(capture: pathlib.Path) -> pathlib.Path:
    return capture.with_name(capture.name + SIDECAR_SUFFIX)


def find_problems(root: pathlib.Path, decoders=DECODERS) -> list[str]:
    """Empty when every capture has a sidecar, every sidecar has a capture and every capture
    directory has a decoder. Each message names the offending path."""
    problems = []
    for capture in captures(root):
        kind = capture.relative_to(root).parts[0]
        if kind not in decoders:
            problems.append(f"{capture}: directory {kind!r} has captures but no decoder")
        if not sidecar_for(capture).exists():
            problems.append(f"{capture}: no sidecar {sidecar_for(capture).name}")
    for sidecar in sorted(root.rglob("*" + SIDECAR_SUFFIX)):
        capture = sidecar.with_name(sidecar.name[:-len(SIDECAR_SUFFIX)])
        if not capture.exists():
            problems.append(f"{sidecar}: sidecar without a capture")
    return problems


def load_payload(path: pathlib.Path) -> bytes:
    """.bin is the raw payload. .hex is hex with an optional '#' comment, whitespace ignored."""
    if path.suffix == ".hex":
        text = path.read_text(encoding="utf-8").split("#", 1)[0]
        return bytes.fromhex("".join(text.split()))
    return path.read_bytes()


# ── Comparison ───────────────────────────────────────────────────────────────────────────────

def resolve(obj, dotted: str):
    """Walk a dotted path: dict keys (an int key is tried for digit segments), list indices."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            elif part.isdigit() and int(part) in cur:
                cur = cur[int(part)]
            else:
                raise KeyError(f"{dotted}: no key {part!r}")
        elif isinstance(cur, (list, tuple)):
            cur = cur[int(part)]
        else:
            raise KeyError(f"{dotted}: cannot index {type(cur).__name__} at {part!r}")
    return cur


def assert_matches(case: unittest.TestCase, want, got, path: str) -> None:
    """Sidecar values: floats to 3 places, "0x..." strings are guids (matched against ints), lists
    element by element with the same length, everything else exact."""
    if isinstance(want, list):
        case.assertIsInstance(got, (list, tuple), path)
        case.assertEqual(len(got), len(want), path)
        for i, (w, g) in enumerate(zip(want, got, strict=True)):
            assert_matches(case, w, g, f"{path}.{i}")
    elif isinstance(want, float):
        case.assertAlmostEqual(got, want, places=3, msg=path)
    elif isinstance(want, str) and want.startswith("0x") and isinstance(got, int):
        case.assertEqual(got, int(want, 16), path)
    else:
        case.assertEqual(got, want, path)


def check_expect(case: unittest.TestCase, decoded, expect: dict) -> None:
    for dotted, want in expect.items():
        try:
            got = resolve(decoded, dotted)
        except (KeyError, IndexError, ValueError) as exc:
            case.fail(f"{dotted}: not decoded ({exc})")
        assert_matches(case, want, got, dotted)


# ── Tests ────────────────────────────────────────────────────────────────────────────────────

class DiscoveryTest(unittest.TestCase):
    def test_committed_tree_has_a_sidecar_for_every_capture(self):
        self.assertEqual(find_problems(FIXTURES), [])

    def test_capture_without_sidecar_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "trade").mkdir()
            (root / "trade" / "lonely.bin").write_bytes(b"\x01")
            problems = find_problems(root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("lonely.bin", problems[0])
        self.assertIn("no sidecar", problems[0])

    def test_sidecar_without_capture_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "mail").mkdir()
            (root / "mail" / "gone.bin.expected.json").write_text("{}")
            problems = find_problems(root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("gone.bin.expected.json", problems[0])
        self.assertIn("without a capture", problems[0])

    def test_capture_directory_without_decoder_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "loot").mkdir()
            (root / "loot" / "pickup.bin").write_bytes(b"\x01")
            (root / "loot" / "pickup.bin.expected.json").write_text("{}")
            problems = find_problems(root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'loot'", problems[0])
        self.assertIn("no decoder", problems[0])

    def test_every_sidecar_has_opcode_source_and_expect(self):
        for capture in captures(FIXTURES):
            with self.subTest(capture=capture.relative_to(FIXTURES).as_posix()):
                sidecar = json.loads(sidecar_for(capture).read_text(encoding="utf-8"))
                self.assertEqual(set(sidecar), {"opcode", "source", "expect"})
                self.assertTrue(sidecar["opcode"].startswith("SMSG_"), sidecar["opcode"])
                self.assertTrue(sidecar["source"].strip())
                self.assertIsInstance(sidecar["expect"], dict)
                self.assertTrue(sidecar["expect"])


class GoldenConformanceTest(unittest.TestCase):
    def test_every_capture_decodes_to_its_sidecar(self):
        for capture in captures(FIXTURES):
            kind = capture.relative_to(FIXTURES).parts[0]
            if kind not in DECODERS:
                continue  # reported by DiscoveryTest
            with self.subTest(capture=capture.relative_to(FIXTURES).as_posix()):
                sidecar = json.loads(sidecar_for(capture).read_text(encoding="utf-8"))
                decoded = DECODERS[kind](load_payload(capture), sidecar["opcode"])
                check_expect(self, decoded, sidecar["expect"])


if __name__ == "__main__":
    unittest.main()
