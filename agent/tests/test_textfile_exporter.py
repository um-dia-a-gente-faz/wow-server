"""#513: monitoring/agent_metrics_textfile.py end to end. main() reads a temp audit dir and
writes a .prom file; the output is checked by a strict Prometheus exposition-format checker
(every sample line is `name{labels} value`, label values escaped, one sample per line, no
duplicate series). The exporter must never raise on file content."""

import json
import os
import re
import tempfile
import unittest

import monitoring.agent_metrics_textfile as exporter

LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\[\\"n])*"'
SAMPLE = re.compile(rf"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{{({LABEL}(?:,{LABEL})*)\}})? (\S+)$")
META = re.compile(r"^# (HELP|TYPE) [a-zA-Z_:][a-zA-Z0-9_:]*( .*)?$")
LABEL_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"')

# every character the issue names, plus a lone surrogate (not encodable as UTF-8)
NASTY = 'q"b\\s\nn\ud800z'
NASTY_SEEN = 'q"b\\s\nn\\ud800z'  # what the exporter writes: the surrogate as backslash-u text, the backslash escaped


def unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), v)


def check_exposition(text: str) -> list[tuple[str, dict, str]]:
    """Strict exposition-format check. Returns (name, labels, value) per sample line."""
    assert text.endswith("\n"), "file must end with a newline"
    samples, seen = [], set()
    for line in text.split("\n")[:-1]:
        if line.startswith("#"):
            assert META.match(line), f"bad comment line: {line!r}"
            continue
        m = SAMPLE.match(line)
        assert m, f"not a single well-formed sample line: {line!r}"
        name, labels, value = m.group(1), m.group(2) or "", m.group(3)
        float(value)  # raises if not a number
        key = (name, labels)
        assert key not in seen, f"duplicate series: {line!r}"
        seen.add(key)
        samples.append((name, {k: unescape(v) for k, v in LABEL_PAIR.findall(labels)}, value))
    return samples


def write_jsonl(path, lines):
    """Each line is a dict (written as JSON, ASCII-escaped) or bytes (written verbatim)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for line in lines:
            f.write(line if isinstance(line, bytes) else json.dumps(line).encode("ascii"))
            f.write(b"\n")


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audit = os.path.join(self._tmp.name, "audit")
        self.out = os.path.join(self._tmp.name, "out", "wow_agent.prom")

    def run_exporter(self):
        self.assertEqual(exporter.main(["--audit-dir", self.audit, "--out", self.out]), 0)
        with open(self.out, encoding="utf-8") as f:
            return check_exposition(f.read())

    @staticmethod
    def rec(agent="A", **kw):
        base = {"schema": 2, "agent": agent, "ts": 1.0, "valid": True, "result": {"ok": True},
                "tool_call": {"name": "face", "args": {}}, "prompt_id": "system@1"}
        base.update(kw)
        return base

    def test_every_label_field_is_escaped_and_the_output_is_strictly_valid(self):
        def pair(**kw):
            # two records an hour apart with an XP span, so every family (xp_per_hour too) is emitted
            return [self.rec(ts=0.0, snapshot={"xp": 10}, **kw), self.rec(ts=3600.0, snapshot={"xp": 20}, **kw)]

        cases = {
            "agent": lambda: pair(agent=NASTY),
            "tool": lambda: pair(tool_call={"name": NASTY, "args": {}}),
            "prompt_id": lambda: pair(prompt_id=NASTY),
        }
        for field, make in cases.items():
            with self.subTest(field=field):
                self.audit = os.path.join(self._tmp.name, field, "audit")
                self.out = os.path.join(self._tmp.name, field, "out", "wow_agent.prom")
                write_jsonl(os.path.join(self.audit, "A", "2026-10-11.jsonl"), make())
                samples = self.run_exporter()
                if field == "agent":
                    self.assertTrue(any(s[1].get("agent") == NASTY_SEEN for s in samples))
                    self.assertTrue(any(s[0] == "wow_agent_xp_per_hour" and s[1].get("agent") == NASTY_SEEN
                                        for s in samples))
                elif field == "tool":
                    self.assertTrue(any(s[0] == "wow_agent_action_total" and s[1].get("action") == NASTY_SEEN
                                        for s in samples))
                else:
                    self.assertTrue(any(s[0] == "wow_agent_decisions_by_prompt_total"
                                        and s[1].get("prompt_id") == NASTY_SEEN for s in samples))

    def test_bad_line_agent_label_is_escaped_from_the_directory_name(self):
        # the agent of a skipped line is its directory; quote, backslash and newline are legal in a Linux name
        dirname = 'q"b\\s\nn'
        write_jsonl(os.path.join(self.audit, dirname, "2026-10-11.jsonl"), [b"{not json"])
        samples = self.run_exporter()
        self.assertEqual([s[2] for s in samples if s[0] == "wow_agent_audit_bad_lines_total"
                          and s[1].get("agent") == dirname], ["1"])

    def test_mixed_file_keeps_good_lines_and_counts_the_bad_ones(self):
        good1 = self.rec(agent="A", prompt_id="p1")
        good2 = self.rec(agent="A", prompt_id="p2", ts=2.0)
        bad = [
            b"[1, 2, 3]",                              # a list
            b"42",                                     # a number
            b'"just a string"',                        # a string
            b"null",                                   # null
            b'{"agent": "A", "ts": 3.0, "valid"',      # truncated JSON
            b'{"agent": "A", "prompt_id": "\xff\xfe"}',  # raw non-UTF-8 bytes inside a string
            b"\xff\xfe\x00 garbage",                   # raw non-UTF-8 bytes
        ]
        write_jsonl(os.path.join(self.audit, "A", "2026-10-11.jsonl"), [good1, *bad[:3], good2, *bad[3:]])
        samples = self.run_exporter()
        self.assertEqual([s[2] for s in samples if s[0] == "wow_agent_audit_cycles_total"
                          and s[1] == {"agent": "A"}], ["2"])
        self.assertEqual([s[2] for s in samples if s[0] == "wow_agent_audit_bad_lines_total"
                          and s[1] == {"agent": "A"}], [str(len(bad))])

    def test_records_of_odd_shapes_do_not_raise(self):
        big = b"1" * 401  # an int JSON allows but a float cannot hold: math.isfinite raises OverflowError on it
        odd = [
            {"agent": ["x"]}, {"agent": "A", "tool_call": "face"}, {"agent": "A", "tool_call": {"name": ["x"]}},
            {"agent": "A", "result": "ok"}, {"agent": "A", "jev_status": ["x"]},
            {"agent": "A", "ts": 1.0, "prompt_tokens": float("inf"), "latency_ms": float("nan")},
            b'{"agent":"A","ts":' + big + b',"valid":true}',
            b'{"agent":"A","ts":1.0,"prompt_tokens":' + big + b'}',
            b'{"agent":"A","ts":1.0,"latency_ms":' + big + b'}',
            b'{"agent":"A","ts":1.0,"snapshot":{"xp":' + big + b'}}',
            b'{"agent":"A","ts":1.0,"brain":"jev","jev_status":"success","usage":{"input_tokens":' + big
            + b',"cost":' + big + b'}}',
        ]
        write_jsonl(os.path.join(self.audit, "A", "2026-10-11.jsonl"), odd)
        self.run_exporter()  # any exception fails the test


if __name__ == "__main__":
    unittest.main()
