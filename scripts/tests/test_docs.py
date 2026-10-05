"""Docs drift guard (#296): relative markdown links and backticked repo paths resolve,
and every doc under docs/ is listed in docs/README.md."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = sorted(ROOT.glob("docs/**/*.md")) + [ROOT / n for n in ("README.md", "CLAUDE.md", "CONTRIBUTING.md")]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TICK = re.compile(r"`([\w.-]+(?:/[\w.-]+)+/?)(?:::\w+)?`")  # `dir/file.py`, `dir/file.py::fn`
TOP = {p.name for p in ROOT.iterdir() if not p.name.startswith(".") or p.name in (".github", ".claude")}


class DocsTest(unittest.TestCase):
    def test_links_and_paths_resolve(self):
        bad = []
        for doc in DOCS:
            text = doc.read_text()
            rel = doc.relative_to(ROOT)
            for target in LINK.findall(text):
                if re.match(r"[a-z]+:|#", target):
                    continue
                if not (doc.parent / target.split("#")[0]).exists():
                    bad.append(f"{rel}: link {target}")
            if rel.parts[:2] == ("docs", "spikes"):
                continue  # archived: paths record what existed then
            for path in TICK.findall(text):
                if path.split("/")[0] in TOP and not (ROOT / path).exists():
                    bad.append(f"{rel}: path {path}")
        self.assertEqual(bad, [], "dangling links or repo paths")

    def test_every_doc_is_indexed(self):
        index = (ROOT / "docs" / "README.md").read_text()
        missing = [
            p.relative_to(ROOT / "docs").as_posix()
            for p in (ROOT / "docs").glob("*.md")
            if p.name != "README.md" and p.name not in index
        ] + [
            p.relative_to(ROOT / "docs").as_posix()
            for p in (ROOT / "docs" / "spikes").glob("*.md")
            if p.name not in index
        ]
        self.assertEqual(missing, [], "doc not listed in docs/README.md")


if __name__ == "__main__":
    unittest.main()
