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
    """Case patterns in the body of stacks_for whose arm body sets game=1 (comment dropped)."""
    m = re.search(r"^stacks_for\(\) \{\n(.*?)^\}$", script, re.S | re.M)
    if not m:
        raise AssertionError("stacks_for not found in deploy.sh")
    patterns, arms = [], 0
    body = [line.split("#")[0] for line in m.group(1).splitlines()]  # comments do not count
    if any(re.search(r";;\s*\S", line) for line in body):
        raise AssertionError("two case arms on one line in stacks_for: put each arm on its own line")
    for line in body:
        arm = re.match(r"\s*([^()\s]+(?:\|[^()\s]+)*)\)(.*)$", line)
        if arm and re.search(r"\bgame=1\b", arm.group(2)):
            patterns += arm.group(1).split("|")
            arms += 1
    # every game=1 in the body must belong to an arm the regex attached it to
    assignments = sum(len(re.findall(r"\bgame=1\b", line)) for line in body)
    if assignments != arms:
        raise AssertionError(f"{assignments - arms} game=1 in stacks_for is not in a parsable case arm")
    if not patterns:
        raise AssertionError("no game-stack arm in stacks_for: the script was restructured")
    return patterns


def gate6(skill):
    start = skill.index("6. **No deploy path.**")
    return skill[start:skill.index("7. **Spacing.**", start)]


def missing_from_gate(script, skill):
    gate = gate6(skill)
    return [p for p in game_patterns(script) if p not in gate]


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
                    "zip/*) mon=1 ;; foo/*) game=1 ;;", "tools/common/*) mon=1 ;; foo/*) game=1 ;;"):
            with self.subTest(arm=arm):
                script = SYNTHETIC.replace("      foo/*) game=1 ;;", "      " + arm)
                with self.assertRaises(AssertionError):
                    game_patterns(script)


if __name__ == "__main__":
    unittest.main()
