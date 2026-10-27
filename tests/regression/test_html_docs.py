"""The HTML versions of the documentation build, and every link in them lands."""

from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

try:
    import markdown_it  # noqa: F401  (comes with docs/requirements.txt)
    import mdit_py_plugins  # noqa: F401
except ImportError:  # pragma: no cover - the docs packages are optional
    markdown_it = None

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(markdown_it is None, "needs the documentation packages (docs/requirements.txt)")
class HtmlDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_html_docs", ROOT / "tools" / "build_html_docs.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.guide, cls.complete = cls.tool.build(Path(cls.tmp.name))
        cls.guide_html = cls.guide.read_text(encoding="utf-8")
        cls.complete_html = cls.complete.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def ids(text):
        return set(re.findall(r'\bid="([^"]+)"', text))

    def test_files_have_the_requested_names(self):
        self.assertEqual(self.guide.name, "OSTRAM_RDM_User_Guide.html")
        self.assertEqual(self.complete.name, "OSTRAM_Complete_Documentation.html")

    def test_every_page_of_the_index_is_in_the_complete_documentation(self):
        pages = [page for _, group in self.tool.toctree(ROOT / "docs" / "index.rst") for page in group]
        self.assertIn("OSTRAM_RDM_User_Guide", pages)
        ids = self.ids(self.complete_html)
        missing = [page for page in pages if self.tool.page_id(page) not in ids]
        self.assertEqual(missing, [])

    def test_in_page_links_land(self):
        for name, text in (("guide", self.guide_html), ("complete", self.complete_html)):
            ids = self.ids(text) | {"top"}
            broken = sorted({a for a in re.findall(r'href="#([^"]+)"', text) if a not in ids})
            self.assertEqual(broken, [], name)

    def test_guide_links_to_other_pages_land_in_the_complete_documentation(self):
        ids = self.ids(self.complete_html)
        targets = re.findall(r'href="OSTRAM_Complete_Documentation\.html#([^"]+)"', self.guide_html)
        self.assertTrue(targets)
        self.assertEqual(sorted({t for t in targets if t not in ids}), [])

    def test_no_markdown_link_is_left_pointing_at_a_md_file(self):
        for text in (self.guide_html, self.complete_html):
            self.assertEqual(re.findall(r'href="(?!https?:)[^"#]*\.md[#"]', text), [])


if __name__ == "__main__":
    unittest.main()
