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

    def test_pages_has_ready_to_publish_section(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Готово к публикации", html)
        self.assertIn("editorial_status==='ready'", html)
        self.assertIn('id="ready"', html)


if __name__ == "__main__":
    unittest.main()
