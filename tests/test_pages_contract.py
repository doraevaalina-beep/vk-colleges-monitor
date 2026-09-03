import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PagesContractTests(unittest.TestCase):
    def test_pages_has_index_entrypoint(self):
        index = ROOT / "docs" / "index.html"
        self.assertTrue(index.is_file(), "docs/index.html is required for the Pages root")
        html = index.read_text(encoding="utf-8")
        self.assertIn("feed.json", html)
        self.assertIn("health.json", html)

    def test_ready_posts_are_rendered_in_separate_section_with_visuals(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Готово к публикации", html)
        self.assertIn('id="ready"', html)
        self.assertIn("editorial_status==='ready'", html)
        self.assertIn("item.visual_url", html)


if __name__ == "__main__":
    unittest.main()
