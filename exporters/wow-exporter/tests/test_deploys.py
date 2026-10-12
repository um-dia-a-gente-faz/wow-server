import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import deploys  # noqa: E402

# Lines as scripts/deploy.sh writes them: `log()` prefixes `$(date -Is)` (ISO-8601 with offset).
DEPLOY_1 = "2026-10-12T19:10:49+00:00 deployed 1a2b3c4, stacks: game monitoring\n"
DEFERRED = "2026-10-12T19:15:00+00:00 DEFERRED game 1a2b3c4, players online: 3\n"
DEPLOY_2 = "2026-10-12T16:20:00-03:00 deployed 5d6e7f8, stacks: monitoring\n"
EPOCH_1 = datetime(2026, 10, 12, 19, 10, 49, tzinfo=UTC).timestamp()
EPOCH_2 = datetime(2026, 10, 12, 19, 20, 0, tzinfo=UTC).timestamp()  # 16:20 at -03:00


def line(i):
    when = datetime(2026, 10, 12, tzinfo=UTC) + timedelta(seconds=i)
    return f"{when.isoformat()} deployed {i:07x}, stacks: game\n"


class ParseDeploysTest(unittest.TestCase):
    def test_real_format_two_deploys_and_a_deferred_line(self):
        got = deploys.parse_deploys(DEPLOY_1 + DEFERRED + DEPLOY_2)
        self.assertEqual(got, [(EPOCH_1, "1a2b3c4", "game+monitoring"),
                               (EPOCH_2, "5d6e7f8", "monitoring")])

    def test_torn_last_line_and_garbage_are_skipped(self):
        text = DEPLOY_1 + "hello world\n" + "2026-10-12T19:30:00+00:00 deployed 1a2b3"
        self.assertEqual(deploys.parse_deploys(text), [(EPOCH_1, "1a2b3c4", "game+monitoring")])

    def test_complete_last_line_without_newline_is_kept(self):
        text = DEPLOY_1 + "2026-10-12T19:30:00+00:00 deployed 9999999, stacks: game"
        self.assertEqual(len(deploys.parse_deploys(text)), 2)

    def test_sha_must_be_7_to_40_lowercase_hex(self):
        bad = ["2026-10-12T19:30:00+00:00 deployed 1a2b3, stacks: game\n",
               "2026-10-12T19:30:00+00:00 deployed " + "a" * 41 + ", stacks: game\n",
               "2026-10-12T19:30:00+00:00 deployed 1A2B3C4, stacks: game\n",
               "2026-10-12T19:30:00+00:00 deployed zzzzzzz, stacks: game\n"]
        self.assertEqual(deploys.parse_deploys("".join(bad)), [])

    def test_stacks_are_words_of_lowercase_and_dash_single_spaced(self):
        bad = ["2026-10-12T19:30:00+00:00 deployed 1a2b3c4, stacks: game  monitoring\n",
               "2026-10-12T19:30:00+00:00 deployed 1a2b3c4, stacks: Game\n",
               "2026-10-12T19:30:00+00:00 deployed 1a2b3c4, stacks: game; rm -rf /\n"]
        self.assertEqual(deploys.parse_deploys("".join(bad)), [])

    def test_naive_time_is_skipped_not_guessed(self):
        self.assertEqual(deploys.parse_deploys("2026-10-12T19:10:49 deployed 1a2b3c4, stacks: game\n"), [])

    def test_limit_keeps_the_newest_deploys(self):
        text = "".join(line(i) for i in range(5))
        got = deploys.parse_deploys(text, limit=2)
        self.assertEqual([sha for _, sha, _ in got], ["0000003", "0000004"])


class ReadDeploysTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name, "deploy.log")

    def test_missing_file_is_empty(self):
        self.assertEqual(deploys.read_deploys(self.path), [])

    def test_reads_the_real_format_file(self):
        self.path.write_text(DEPLOY_1 + DEFERRED + DEPLOY_2)
        self.assertEqual(len(deploys.read_deploys(self.path)), 2)

    def test_file_larger_than_the_tail_window_returns_newest_whole_lines(self):
        # ~2000 lines of ~60 bytes: well past the 64 KiB window, so the read starts mid-file.
        self.path.write_text("".join(line(i) for i in range(2000)))
        got = deploys.read_deploys(self.path, limit=3)
        self.assertEqual([sha for _, sha, _ in got], ["00007cd", "00007ce", "00007cf"])
        self.assertEqual([stacks for _, _, stacks in got], ["game"] * 3)

    def test_torn_tail_while_deploy_sh_appends_is_not_a_deploy(self):
        self.path.write_text(DEPLOY_1 + "2026-10-12T19:30:00+00:00 deployed 9")
        self.assertEqual(deploys.read_deploys(self.path), [(EPOCH_1, "1a2b3c4", "game+monitoring")])


if __name__ == "__main__":
    unittest.main()
