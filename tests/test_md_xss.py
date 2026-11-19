"""Маленький markdown пульта не даёт недоверенному тексту выйти из атрибута ссылки (XSS 16.11)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """
const {mdToHtml} = await import(process.argv[1]);
const out = JSON.parse(process.argv[2]).map((t) => mdToHtml(t));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "нужен node")
class MdXssTest(unittest.TestCase):
    def render(self, texts):
        with tempfile.TemporaryDirectory() as d:
            mod = Path(d) / "md.mjs"
            mod.write_text((ROOT / "bender/web/static/lib/md.js").read_text(encoding="utf-8"), encoding="utf-8")
            r = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, mod.as_uri(), json.dumps(texts)], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

    def test_autolink_inside_href_does_not_break_attribute(self):
        evil = ["[тут](https://a.com/(https://x/onmouseover=window.__pwned=1+)",
                "[лог](https://ci.example/(https://x/onmouseover=location='javascript:void%28x%29'+)",
                "https://a.org/\"onmouseover=alert(1)"]
        for html in self.render(evil):
            self.assertEqual(html.count("<a "), 1, html)
            # Всё от href=" до закрывающей кавычки — одно значение, без новых атрибутов.
            tag = html[html.index("<a "):html.index(">", html.index("<a "))]
            self.assertNotIn("onmouseover=", tag.split('" target=')[1], html)
            self.assertEqual(tag.count('"'), 6, html)

    def test_ordinary_links_still_work(self):
        [html] = self.render(["просто https://example.org и [ссылка](https://b.org/p?q=1) (https://c.org)"])
        self.assertIn('<a href="https://example.org"', html)
        self.assertIn('<a href="https://b.org/p?q=1"', html)
        self.assertIn('>ссылка</a>', html)
        self.assertIn('(<a href="https://c.org"', html)


if __name__ == "__main__":
    unittest.main()
