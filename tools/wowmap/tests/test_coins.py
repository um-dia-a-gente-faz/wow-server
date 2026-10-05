"""#177: one money renderer (Coins.render) for the drawer, item tooltip and activity feed."""
import json
import pathlib
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402

NODE = unittest.skipUnless(shutil.which("node"), "node is not installed")

# A just-enough DOM: elements with className/title/attributes/children and a text dump.
HARNESS = """
class N { constructor() { this.className = ''; this.kids = []; this.attrs = {}; this.title = ''; }
  setAttribute(k, v) { this.attrs[k] = v; } append(...k) { this.kids.push(...k); }
  get text() { return (this.textContent ?? '') + this.kids.map((k) => k.text).join(''); } }
const document = { createElement: () => new N() };
"""


def run_js(body):
    src = app.PAGE[app.PAGE.index("const Coins = "):app.PAGE.index("// In-game style item tooltip")]
    r = subprocess.run(["node", "-e", HARNESS + src + body], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class CoinsTests(unittest.TestCase):
    @NODE
    def test_split_keeps_copper_exact(self):
        out = run_js("""console.log(JSON.stringify([12345, 0, 99, 100, 10000, 1234567, -5, 'x', 12.9]
          .map((c) => Coins.split(c))));""")
        self.assertEqual(out, [[1, 23, 45], [0, 0, 0], [0, 0, 99], [0, 1, 0], [1, 0, 0],
                               [123, 45, 67], [0, 0, 0], [0, 0, 0], [0, 0, 12]])

    @NODE
    def test_render_omits_zero_denominations_and_names_a_zero_purse(self):
        out = run_js("""const f = (c) => { const n = Coins.render(c);
          return [n.text, n.kids.filter((k) => k.className.startsWith('coin')).map((k) => k.className),
                  n.title, n.attrs['aria-label']]; };
          console.log(JSON.stringify([f(12345), f(1200), f(10000), f(0), f(1234567890)]));""")
        self.assertEqual(out, [
            ["12345", ["coin g", "coin s", "coin c"], "12,345 copper", "1 gold 23 silver 45 copper"],
            ["12", ["coin s"], "1,200 copper", "12 silver"],
            ["1", ["coin g"], "10,000 copper", "1 gold"],
            ["0", ["coin c"], "0 copper", "0 copper"],
            ["123,4567890", ["coin g", "coin s", "coin c"], "1,234,567,890 copper",
             "123456 gold 78 silver 90 copper"],
        ])

    def test_every_call_site_uses_the_shared_renderer(self):
        for call in ("left.append(' ', Coins.render(l.money))",      # item tooltip
                     "kv('Gold', Coins.render(c.money))",             # drawer
                     "tx.append(Coins.render(Math.abs(e.detail.delta)))"):  # activity feed
            self.assertIn(call, app.PAGE)
        for old in ("function money(", "function coins(", "coins(Number("):
            self.assertNotIn(old, app.PAGE)


if __name__ == "__main__":
    unittest.main()
