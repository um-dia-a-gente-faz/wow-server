"""#175: the live-map player row shows name, class badge with the level, and zone only."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pagesrc  # noqa: E402
import players  # noqa: E402


def js_fn(name, end):
    """Source of a page function, from its `function <name>` up to the `end` marker."""
    start = pagesrc.PAGE.index(f"function {name}(")
    return pagesrc.PAGE[start:pagesrc.PAGE.index(end, start)]


# A tiny DOM, enough for renderList to build its rows.
FAKE_DOM = r"""
const node = () => ({style: {}, dataset: {}, children: [], className: '', textContent: '', title: '', attrs: {},
                     append(...c) { this.children.push(...c); }, setAttribute(k, v) { this.attrs[k] = v; }});
const document = {createElement: node};
const list = Object.assign(node(), {innerHTML: '', appendChild(c) { this.children.push(c); }});
const $ = id => id === 'list' ? list : {value: ''};
const Inspect = {current: () => null};
const selected = [];
const selectCharacter = n => selected.push(n);
let players = JSON.parse(process.argv[2]);
renderList();
for (const key of ['Enter', ' ', 'a']) list.children[0].onkeydown({key, preventDefault() {}});
console.log(JSON.stringify(list.children.map(r => ({
  title: r.title, tabIndex: r.tabIndex, attrs: r.attrs, selected,
  cells: r.children.map(c => ({cls: c.className, text: String(c.textContent), bg: c.style.background, ink: c.style.color})),
}))));
"""

PLAYERS = [
    {"name": "Kaelthasidus", "level": 12, "class_name": "Warrior", "class_color": "#C79C6E",
     "race_name": "Blood Elf", "in_world": True, "continent_name": "Eastern Kingdoms",
     "zone_name": "Eversong Woods", "subzone_name": "Silvermoon City",
     "map_coords": {"x": 34.2, "y": 61.8}},
    {"name": "Luaprata", "level": 7, "class_name": "Mage", "class_color": "#69CCF0",
     "race_name": "Human", "in_world": False, "continent_name": "Deadmines",
     "zone_name": "The Deadmines", "subzone_name": None, "map_coords": None},
]


def contrast(a, b):
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PlayerRowTests(unittest.TestCase):
    def rows(self, players=PLAYERS):
        # placeText, mapCoordsText and playerTip sit together before worldText.
        script = (js_fn("placeText", "\nfunction worldText")
                  + js_fn("renderList", "\nasync function tick") + FAKE_DOM)
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "row.js"
            f.write_text(script)
            r = subprocess.run(["node", str(f), json.dumps(players)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_row_is_badge_with_level_name_and_zone_only(self):
        world, inst = self.rows()
        self.assertEqual([c["cls"] for c in world["cells"]], ["dot", "nm", "meta"])
        badge, name, meta = world["cells"]
        self.assertEqual(badge["text"], "12")
        self.assertEqual(badge["bg"], "#C79C6E")
        self.assertEqual(name["text"], "Kaelthasidus")
        self.assertEqual(meta["text"], "Eversong Woods")
        self.assertEqual(inst["cells"][0]["text"], "7")
        self.assertEqual(inst["cells"][2]["text"], "Deadmines (instance)")
        for row in (world, inst):
            text = " ".join(c["text"] for c in row["cells"])
            self.assertNotIn("Warrior", text)
            self.assertNotIn("Mage", text)
            self.assertNotIn("34.2", text)

    def test_row_tooltip_keeps_class_race_level_and_coords(self):
        world, inst = self.rows()
        for part in ("Kaelthasidus", "Warrior", "Blood Elf", "lvl 12", "Eversong Woods", "34.2, 61.8"):
            self.assertIn(part, world["title"])
        for part in ("Mage", "Human", "lvl 7", "Deadmines"):
            self.assertIn(part, inst["title"])

    def test_row_details_reach_keyboard_and_screen_readers(self):
        world, inst = self.rows()
        for row in (world, inst):
            self.assertEqual(row["tabIndex"], 0)
            self.assertEqual(row["attrs"]["role"], "button")
            self.assertEqual(row["attrs"]["aria-label"], row["title"].replace("\n", ", "))
        self.assertIn("Warrior Blood Elf lvl 12", world["attrs"]["aria-label"])
        self.assertIn("34.2, 61.8", world["attrs"]["aria-label"])
        self.assertEqual(world["selected"], ["Kaelthasidus", "Kaelthasidus"])  # Enter and Space, not 'a'

    def test_badge_numeral_contrast_is_at_least_4_5_for_every_class(self):
        colours = list(players.CLASS_COLORS.values()) + ["#888888"]  # #888888: unknown-class fallback
        rows = self.rows([dict(PLAYERS[0], class_color=c) for c in colours])
        for colour, row in zip(colours, rows):
            ink = row["cells"][0].get("ink") or "#10131a"  # unset inline: the CSS var(--bg)
            with self.subTest(colour=colour, ink=ink):
                self.assertGreaterEqual(contrast(colour, ink), 4.5)

    def test_marker_tooltip_uses_the_same_text(self):
        self.assertTrue("e.title = playerTip(p);" in pagesrc.PAGE)


class BadgeCssTests(unittest.TestCase):
    def test_row_badge_has_a_fixed_size_for_two_digits(self):
        self.assertTrue(".pl .dot { width:20px; height:20px;" in pagesrc.PAGE)


if __name__ == "__main__":
    unittest.main()
