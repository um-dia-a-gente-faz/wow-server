"""#175: the live-map player row shows name, class badge with the level, and zone only."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402


def js_fn(name, end):
    """Source of a page function, from its `function <name>` up to the `end` marker."""
    start = app.PAGE.index(f"function {name}(")
    return app.PAGE[start:app.PAGE.index(end, start)]


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
  cells: r.children.map(c => ({cls: c.className, text: String(c.textContent), bg: c.style.background})),
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


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PlayerRowTests(unittest.TestCase):
    def rows(self):
        # placeText, mapCoordsText and playerTip sit together before worldText.
        script = (js_fn("placeText", "\nfunction worldText")
                  + js_fn("renderList", "\nasync function tick") + FAKE_DOM)
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "row.js"
            f.write_text(script)
            r = subprocess.run(["node", str(f), json.dumps(PLAYERS)], capture_output=True, text=True)
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

    def test_marker_tooltip_uses_the_same_text(self):
        self.assertTrue("e.title = playerTip(p);" in app.PAGE)


class BadgeCssTests(unittest.TestCase):
    def test_row_badge_has_a_fixed_size_for_two_digits(self):
        self.assertTrue(".pl .dot { width:20px; height:20px;" in app.PAGE)


if __name__ == "__main__":
    unittest.main()
