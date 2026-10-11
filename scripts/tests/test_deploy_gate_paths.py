"""#549: every game-stack path of scripts/deploy.sh `stacks_for` appears in milestone-loop gate 6.

Reads the script and the skill as text; nothing runs.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy.sh"
SKILL = ROOT / ".claude" / "skills" / "milestone-loop" / "SKILL.md"


def game_patterns(script):
    """Case patterns in the body of stacks_for whose arm body sets game=1 (comments are not parsed, but a game=1 inside one still counts and raises)."""
    m = re.search(r"^stacks_for\(\) \{\n(.*?)^\}$", script, re.S | re.M)
    if not m:
        raise AssertionError("stacks_for not found in deploy.sh")
    if any(line.rstrip().endswith("\\") for line in m.group(1).splitlines()):
        raise AssertionError("backslash-continued line in stacks_for: put each pattern arm on one line")
    if any(re.search(r"\bgame\+?=(?!0\b|1\b)", line) for line in m.group(1).splitlines()):
        raise AssertionError("game assigned other than literal 0 or 1 in stacks_for: use game=1 or game=0")
    patterns, arms = [], 0
    body = [line.split("#")[0] for line in m.group(1).splitlines()]  # comments do not count
    if any(re.search(r";&\s*\S|;;(?!\s*esac\s*$)\s*\S", line) for line in body):
        raise AssertionError("two case arms on one line in stacks_for: put each arm on its own line")
    if sum(bool(re.search(r"\bcase\b", line)) for line in body) > 1:
        raise AssertionError("nested case in stacks_for: only the stacks_for case may be parsed")
    for line in body:
        arm = re.match(r"\s*([^()\s]+(?:\|[^()\s]+)*)\)(.*)$", line)
        if arm and re.search(r"\bgame=1\b", arm.group(2)):
            patterns += arm.group(1).split("|")
            arms += 1
    # every game=1 in the body must belong to an arm the regex attached it to
    assignments = sum(bool(re.search(r"\bgame=1\b", line)) for line in m.group(1).splitlines())
    if assignments != arms:
        raise AssertionError(f"{assignments - arms} game=1 in stacks_for is not in a parsable case arm")
    if not patterns:
        raise AssertionError("no game-stack arm in stacks_for: the script was restructured")
    return patterns


def gate6(skill):
    start = skill.index("6. **No deploy path.**")
    end = skill.index("7. **Spacing.**", start)
    not_gated = skill.find("Not gated:", start, end)
    return skill[start:end if not_gated < 0 else not_gated]


def missing_from_gate(script, skill):
    gate = gate6(skill)
    return [p for p in game_patterns(script) if f"`{p}`" not in gate]


SYNTHETIC = """stacks_for() {
  local f game=0 mon=0
  while IFS= read -r -d '' f; do
    case "$f" in
      docker-compose.yml|tdb/*|tools/chat-feed/*) game=1 ;;
      foo/*) game=1 ;;
      tools/common/*) game=1; mon=1 ;;  # copied into both images (ADR 0014)
      bar/*) mon=1 ;;
    esac
  done
}
"""


class GatePathsTest(unittest.TestCase):
    def test_every_game_stack_path_is_in_gate_6(self):
        self.assertEqual(missing_from_gate(DEPLOY.read_text(), SKILL.read_text()), [])

    def test_tools_common_arm_is_a_game_pattern(self):
        self.assertIn("tools/common/*", game_patterns(DEPLOY.read_text()))

    def test_synthetic_game_arm_missing_from_gate_is_reported(self):
        self.assertEqual(missing_from_gate(SYNTHETIC, SKILL.read_text()), ["foo/*"])

    def test_monitoring_only_arm_is_not_extracted(self):
        self.assertNotIn("bar/*", game_patterns(SYNTHETIC))

    def test_case_statements_outside_stacks_for_are_ignored(self):
        script = SYNTHETIC + 'case "$x" in\n  zzz/*) game=1 ;;\nesac\n'
        self.assertNotIn("zzz/*", game_patterns(script))

    def test_no_game_arm_fails_instead_of_passing_empty(self):
        with self.assertRaises(AssertionError):
            game_patterns("stacks_for() {\n  :\n}\n")

    def test_trailing_comment_after_arm_terminator_is_fine(self):
        script = SYNTHETIC.replace("      foo/*) game=1 ;;", "      foo/*) game=1 ;;  # note")
        self.assertIn("foo/*", game_patterns(script))

    def test_game_assignment_no_arm_regex_matches_fails(self):
        # the game=1 is still in stacks_for; the arm line is not one the regex can attach it to
        for arm in ("foo/*)\n      game=1 ;;", "foo/* ) game=1 ;;", "a/* | b/*) game=1 ;;",
                    "zip/*) mon=1 ;; foo/*) game=1 ;;", "tools/common/*) mon=1 ;; foo/*) game=1 ;;",
                    "a/*) mon=1 ;& foo/*) game=1 ;;", '"a#b"/*) game=1 ;;'):
            with self.subTest(arm=arm):
                script = SYNTHETIC.replace("      foo/*) game=1 ;;", "      " + arm)
                with self.assertRaises(AssertionError):
                    game_patterns(script)

    # #567: one test per rule, synthetic input.

    def test_gate_text_stops_at_not_gated(self):  # rule 1
        skill = ("6. **No deploy path.** `docker-compose.yml` `tdb/*` `tools/chat-feed/*`\n"
                 "   Not gated: `tools/common/*`, the monitoring paths.\n7. **Spacing.** x\n")
        self.assertEqual(missing_from_gate(DEPLOY.read_text(), skill), ["tools/common/*"])

    def test_pattern_must_be_a_whole_backticked_token(self):  # rule 2
        script = SYNTHETIC.replace("      bar/*) mon=1 ;;", "      common/*) game=1 ;;")
        skill = ("6. **No deploy path.** `docker-compose.yml` `tdb/*` `tools/chat-feed/*` "
                 "`tools/common/*` foo/*\n7. **Spacing.**")
        self.assertEqual(missing_from_gate(script, skill), ["foo/*", "common/*"])

    def test_backslash_continued_pattern_list_raises(self):  # rule 3
        script = SYNTHETIC.replace("      docker-compose.yml|tdb/*|tools/chat-feed/*) game=1 ;;",
                                   "      docker-compose.yml|tdb/*|\\\n        tools/chat-feed/*) game=1 ;;")
        with self.assertRaises(AssertionError):
            game_patterns(script)

    def test_game_assigned_other_than_0_or_1_raises(self):  # rule 4
        for arm in ('foo/*) game="1" ;;', "foo/*) game=true ;;", "foo/*) game=$x ;;"):
            with self.subTest(arm=arm):
                script = SYNTHETIC.replace("      foo/*) game=1 ;;", "      " + arm)
                with self.assertRaises(AssertionError):
                    game_patterns(script)

    def test_two_assignments_in_one_arm_count_once(self):  # rule 5
        script = SYNTHETIC.replace("      foo/*) game=1 ;;", "      foo/*) game=1; game=1 ;;")
        self.assertEqual(game_patterns(script).count("foo/*"), 1)

    def test_esac_after_arm_terminator_on_same_line_is_fine(self):  # rule 6
        script = SYNTHETIC.replace("      foo/*) game=1 ;;", "      foo/*) game=1 ;; esac")
        self.assertIn("foo/*", game_patterns(script))

    def test_nested_case_in_stacks_for_raises(self):  # rule 7
        script = SYNTHETIC.replace("      bar/*) mon=1 ;;\n",
                                   "      bar/*) mon=1 ;;\n      baz/*) case \"$g\" in\n"
                                   "        q/*) mon=1 ;;\n      esac ;;\n")
        with self.assertRaises(AssertionError):
            game_patterns(script)


if __name__ == "__main__":
    unittest.main()
