import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import editorial_runner as editorial


def queue_entry(status="ready", **changes):
    entry = {
        "status": status,
        "rewrite": "Готовый редакционный текст",
        "template": "templates/04_dostizhenie.svg",
        "headline": "Студент — второй в России",
        "detail": "Профессионалы • тестирование ПО",
    }
    entry.update(changes)
    return entry


class EditorialQueueTests(unittest.TestCase):
    def test_loads_valid_queue_and_reports_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial_queue.json"
            path.write_text(
                json.dumps({"a": queue_entry(), "b": queue_entry("sent"), "c": queue_entry("skip")}),
                encoding="utf-8",
            )
            health = {}
            queue = editorial.load_editorial_queue(path, health)
        self.assertEqual(len(queue), 3)
        self.assertTrue(health["editorial_queue_loaded"])
        self.assertEqual(health["editorial_ready_items"], 1)
        self.assertEqual(health["editorial_sent_items"], 1)
        self.assertEqual(health["editorial_skipped_items"], 1)

    def test_missing_or_invalid_queue_is_safe(self):
        health = {}
        self.assertEqual(editorial.load_editorial_queue(Path("does-not-exist.json"), health), {})
        self.assertFalse(health["editorial_queue_loaded"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial_queue.json"
            path.write_text("{broken", encoding="utf-8")
            health = {}
            self.assertEqual(editorial.load_editorial_queue(path, health), {})
            self.assertFalse(health["editorial_queue_loaded"])
            self.assertTrue(health["editorial_errors"])

    def test_rewrite_is_added_for_new(self):
        item = {"id": "a", "attachments": []}
        editorial.apply_editorial_queue([item], {"a": queue_entry("new")}, {})
        self.assertEqual(item["rewrite"], "Готовый редакционный текст")
        self.assertEqual(item["editorial_status"], "new")
        self.assertNotIn("visual", item)

    def test_ready_creates_visual_with_first_local_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            images = Path(directory)
            (images / "a.jpg").write_bytes(b"photo")
            (images / "b.jpg").write_bytes(b"photo")
            item = {
                "id": "a",
                "attachments": [
                    {"type": "photo", "mirror_url": "https://site/images/a.jpg"},
                    {"type": "photo", "mirror_url": "https://site/images/b.jpg"},
                ],
            }
            health = {}
            editorial.apply_editorial_queue([item], {"a": queue_entry()}, health, images)
        self.assertEqual(item["visual"]["original_photo"], str(images / "a.jpg"))
        self.assertEqual(health["editorial_errors"], [])

    def test_incomplete_ready_does_not_create_visual(self):
        item = {"id": "a", "attachments": []}
        health = {}
        editorial.apply_editorial_queue(
            [item], {"a": queue_entry(template="", headline="", detail="")}, health
        )
        self.assertNotIn("visual", item)
        self.assertIn("template, headline, detail, original_photo", health["editorial_errors"][0]["error"])

    def test_ready_without_nonempty_rewrite_clears_stale_visual(self):
        item = {
            "id": "a",
            "attachments": [],
            "visual": {"old": True},
            "visual_url": "old",
            "visual_fingerprint": "old",
        }
        health = {}
        editorial.apply_editorial_queue([item], {"a": queue_entry(rewrite="  ")}, health)
        self.assertNotIn("visual", item)
        self.assertNotIn("visual_url", item)
        self.assertNotIn("visual_fingerprint", item)
        self.assertIn("rewrite", health["editorial_errors"][0]["error"])

    def test_ready_duplicate_text_forces_own_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images"
            uploads = root / "uploads"
            queue = root / "editorial_queue.json"
            queue.write_text(json.dumps({"ready": queue_entry()}), encoding="utf-8")
            items = [
                {"id": "first", "text": "Одинаковый текст", "attachments": [
                    {"type": "photo", "url": "https://vk/first.jpg"}
                ]},
                {"id": "ready", "text": "Одинаковый текст", "attachments": [
                    {"type": "photo", "url": "https://vk/ready.jpg"}
                ]},
            ]

            def fake_download(session, source_url, target):
                target.write_bytes(source_url.encode("utf-8"))

            with mock.patch.object(editorial, "EDITORIAL_QUEUE_FILE", queue), mock.patch.multiple(
                editorial.monitor, PUBLIC_IMAGE_DIR=images, IMAGE_UPLOAD_DIR=uploads,
            ), mock.patch.object(
                editorial.monitor, "download_original_photo", side_effect=fake_download
            ):
                health = {}
                editorial.mirror_feed_photos_with_editorial(items, object(), health)

            self.assertTrue((images / "ready_1.jpg").is_file())
            self.assertIn("mirror_url", items[1]["attachments"][0])
            self.assertEqual(items[1]["visual"]["original_photo"], str(images / "ready_1.jpg"))

    def test_loaded_queue_clears_removed_entry(self):
        item = {
            "id": "removed",
            "editorial_status": "ready",
            "rewrite": "Старый текст",
            "visual": {"old": True},
            "visual_url": "old",
            "visual_fingerprint": "old",
        }
        editorial.apply_editorial_queue([item], {}, {"editorial_queue_loaded": True})
        for field in ("editorial_status", "rewrite", "visual", "visual_url", "visual_fingerprint"):
            self.assertNotIn(field, item)

    def test_failed_queue_load_preserves_previous_fields(self):
        item = {
            "id": "preserved",
            "editorial_status": "ready",
            "rewrite": "Старый текст",
            "visual": {"old": True},
            "visual_url": "old",
            "visual_fingerprint": "old",
        }
        before = dict(item)
        editorial.apply_editorial_queue([item], {}, {"editorial_queue_loaded": False})
        self.assertEqual(item, before)

    def test_skip_and_sent_are_not_ready(self):
        skip = {"id": "skip", "attachments": [], "visual": {"old": True}, "visual_url": "old"}
        sent = {"id": "sent", "attachments": []}
        editorial.apply_editorial_queue(
            [skip, sent],
            {"skip": queue_entry("skip"), "sent": queue_entry("sent")},
            {},
        )
        self.assertNotIn("visual", skip)
        self.assertNotIn("visual_url", skip)
        self.assertEqual(sent["editorial_status"], "sent")
        self.assertNotIn("visual", sent)

    def test_invalid_status_is_controlled_error(self):
        item = {"id": "a", "attachments": []}
        health = {}
        editorial.apply_editorial_queue([item], {"a": queue_entry("unknown")}, health)
        self.assertNotIn("editorial_status", item)
        self.assertIn("Недопустимый status", health["editorial_errors"][0]["error"])

    def test_malformed_entry_clears_stale_ready(self):
        item = {
            "id": "a", "editorial_status": "ready", "rewrite": "Старый текст",
            "visual": {"old": True}, "visual_url": "old", "visual_fingerprint": "old",
        }
        health = {"editorial_queue_loaded": True}
        editorial.apply_editorial_queue([item], {"a": []}, health)
        for field in ("editorial_status", "rewrite", "visual", "visual_url", "visual_fingerprint"):
            self.assertNotIn(field, item)
        self.assertIn("должна быть объектом", health["editorial_errors"][0]["error"])

    def test_invalid_status_clears_stale_ready(self):
        item = {
            "id": "a", "editorial_status": "ready", "rewrite": "Старый текст",
            "visual": {"old": True}, "visual_url": "old", "visual_fingerprint": "old",
        }
        health = {"editorial_queue_loaded": True}
        editorial.apply_editorial_queue([item], {"a": queue_entry("unknown")}, health)
        for field in ("editorial_status", "rewrite", "visual", "visual_url", "visual_fingerprint"):
            self.assertNotIn(field, item)
        self.assertIn("Недопустимый status", health["editorial_errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
