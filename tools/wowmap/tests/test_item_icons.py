import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import item_icons  # noqa: E402

try:
    from PIL import Image
except ImportError:  # CI installs only requirements.txt; Pillow is an extraction-time dep
    Image = None


def make_item_display_dbc(records, fields=25):
    """A WDBC file shaped like 12340's ItemDisplayInfo.dbc: {id: icon name or ''}."""
    strings = bytearray(b"\x00")
    rows = []
    for display_id, icon in records.items():
        row = [0] * fields
        row[0] = display_id
        row[1] = len(strings)            # ModelName[0], must not be mistaken for the icon
        strings += b"model.mdx\x00"
        if icon:
            row[5] = len(strings)        # InventoryIcon[0]
            strings += icon.encode() + b"\x00"
        rows.append(struct.pack(f"<{fields}I", *row))
    header = struct.pack("<4sIIII", b"WDBC", len(rows), fields, fields * 4, len(strings))
    return header + b"".join(rows) + bytes(strings)


class ParseTests(unittest.TestCase):
    def test_field_5_is_the_inventory_icon(self):
        data = make_item_display_dbc({6418: "INV_Misc_Rune_01", 2380: "INV_Sword_06", 7: ""})
        self.assertEqual(item_icons.parse_display_icons(data),
                         {6418: "INV_Misc_Rune_01", 2380: "INV_Sword_06"})

    def test_rejects_other_layouts(self):
        with self.assertRaises(ValueError):
            item_icons.parse_display_icons(make_item_display_dbc({1: "x"}, fields=24))
        with self.assertRaises(ValueError):
            item_icons.parse_display_icons(b"WDBX" + make_item_display_dbc({1: "x"})[4:])

    def test_icon_file_is_lowercase_and_path_safe(self):
        self.assertEqual(item_icons.icon_file("INV_Sword_04"), "inv_sword_04.png")
        self.assertEqual(item_icons.icon_file("Interface\\Icons\\INV_Misc_Rune_01.blp"),
                         "inv_misc_rune_01.png")
        self.assertEqual(item_icons.icon_file("../../etc/passwd"), "passwd.png")
        self.assertEqual(item_icons.icon_file("Ability Seal"), "ability_seal.png")
        self.assertRegex(item_icons.icon_file("INV_Sword_04"), app.ICON_FILE_RE)

    def test_mpq_path(self):
        self.assertEqual(item_icons.mpq_path("INV_Sword_04"), "Interface\\Icons\\INV_Sword_04.blp")


class IconUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "inv_sword_06.png"), "wb") as f:
            f.write(b"\x89PNG fake")
        for p in (mock.patch.object(app, "ICONS_DIR", self.tmp.name),
                  mock.patch.object(app, "_display_icons",
                                    {2380: "INV_Sword_06", 6418: "INV_Misc_Rune_01"})):
            p.start()
            self.addCleanup(p.stop)

    def test_url_when_the_png_was_extracted(self):
        self.assertEqual(app.icon_url(2380), "/icons/inv_sword_06.png")

    def test_none_when_png_missing_or_display_unknown(self):
        self.assertIsNone(app.icon_url(6418))   # in the DBC, not extracted
        self.assertIsNone(app.icon_url(99999))  # not in the DBC
        self.assertIsNone(app.icon_url(None))
        self.assertIsNone(app.icon_url(0))

    def test_missing_dbc_disables_icons_without_failing(self):
        with mock.patch.object(app, "_display_icons", None), \
                mock.patch.object(app, "DBC_DIR", self.tmp.name):
            self.assertEqual(app.display_icons(), {})
            self.assertIsNone(app.icon_url(2380))

    def test_loads_the_dbc_from_dbc_dir(self):
        with open(os.path.join(self.tmp.name, "ItemDisplayInfo.dbc"), "wb") as f:
            f.write(make_item_display_dbc({2380: "INV_Sword_06"}))
        with mock.patch.object(app, "_display_icons", None), \
                mock.patch.object(app, "DBC_DIR", self.tmp.name):
            self.assertEqual(app.icon_url(2380), "/icons/inv_sword_06.png")


class IconRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "inv_sword_06.png"), "wb") as f:
            f.write(b"\x89PNG fake")
        with open(os.path.join(self.tmp.name, "secret.txt"), "wb") as f:
            f.write(b"no")
        p = mock.patch.object(app, "ICONS_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_serves_png_with_long_cache(self):
        status, headers, body = self.get("/icons/inv_sword_06.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG fake")
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertIn("max-age=2592000", headers["Cache-Control"])

    def test_404_for_missing_or_unexpected_names(self):
        for path in ("/icons/inv_missing.png", "/icons/secret.txt", "/icons/../app.py",
                     "/icons/%2e%2e/app.py", "/icons/INV_Sword_06.png", "/icons/"):
            self.assertEqual(self.get(path)[0], 404, path)


@unittest.skipIf(Image is None, "Pillow not installed (only needed to extract icons)")
class DecodeTests(unittest.TestCase):
    def test_decodes_a_blp2_icon(self):
        import extract_icons
        # Pillow writes palettised BLP2, the same encoding many client icons use.
        src = Image.new("RGB", (64, 64), (200, 30, 30)).convert(
            "P", palette=Image.Palette.ADAPTIVE)
        buf = io.BytesIO()
        src.save(buf, "BLP")
        self.assertEqual(buf.getvalue()[:4], b"BLP2")
        img = extract_icons.decode_blp(buf.getvalue())
        self.assertEqual((img.size, img.mode), ((64, 64), "RGBA"))
        self.assertEqual(img.getpixel((10, 10))[:3], (200, 30, 30))

    def test_rejects_garbage(self):
        import extract_icons
        with self.assertRaises(Exception):
            extract_icons.decode_blp(b"not a blp at all")


class PageTests(unittest.TestCase):
    def test_drawer_renders_icons_from_the_api_field(self):
        self.assertIn("function itemIcon(it)", app.PAGE)
        self.assertIn("it.icon", app.PAGE)

    def test_empty_slot_is_the_same_box_as_a_real_icon(self):
        # No size on .empty beyond the shared .ico box, so no reflow between states.
        body = re.search(r"\.drawer \.ico\.empty\s*\{([^}]*)\}", app.PAGE).group(1)
        self.assertNotRegex(body, r"(?<![-\w])(width|height|flex)\s*:")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_item_icon_states_in_a_stub_dom(self):
        src = app.PAGE[app.PAGE.index("const Q_COLORS"):app.PAGE.index("function itemRows")]
        js = """
        class N { constructor(){ this.classList = new Set(); this.kids = []; this.style = {}; this.l = {};
          this.attrs = {}; }
          append(...k){ this.kids.push(...k); } remove(){ this.gone = true; }
          addEventListener(e, f){ this.l[e] = f; } setAttribute(k, v){ this.attrs[k] = v; } }
        const el = (t, c, txt) => { const n = new N(); n.tag = t; (c||'').split(' ').filter(Boolean)
          .forEach((x) => n.classList.add(x)); n.text = txt; return n; };
        const ItemTip = { attach(){} };
        """ + src + """
        const base = {item_name: 'Sword', quality: 3, count: 1};
        const ok = itemIcon({...base, icon: '/icons/a.png'});
        const none = itemIcon({...base, icon: null});
        const bad = itemIcon({...base, icon: '/icons/gone.png'});
        bad.kids[0].l.error();
        console.log(JSON.stringify({
          ok: [ok.kids[0].src, ok.classList.has('ph')],
          none: [none.kids.length, none.classList.has('ph'), none.classList.has('empty')],
          bad: [bad.kids[0].gone, bad.classList.has('ph')]}));
        """
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"ok": ["/icons/a.png", False],
                                                "none": [0, True, False],
                                                "bad": [True, True]})


if __name__ == "__main__":
    unittest.main()
