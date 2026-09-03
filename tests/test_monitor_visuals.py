import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monitor


class MonitorVisualTests(unittest.TestCase):
    def test_builder_version_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.jpg"
            archive = root / "sources.zip"
            font = root / "onest.woff2"
            builder_source = root / "visual_builder.py"
            for path, content in ((photo, b"photo"), (archive, b"svg"),
                                  (font, b"font"), (builder_source, b"version 1")):
                path.write_bytes(content)
            item = {"id": "-1_1"}
            request = {"template": "templates/a.svg", "original_photo": str(photo),
                       "headline": "h", "detail": "d"}
            with mock.patch.multiple(
                monitor, VISUAL_ARCHIVE=archive, VISUAL_FONT=font,
                VISUAL_BUILDER_SOURCE=builder_source,
            ):
                first = monitor.visual_fingerprint(item, request)
                builder_source.write_bytes(b"version 2")
                second = monitor.visual_fingerprint(item, request)
        self.assertNotEqual(first, second)

    def test_builder_failure_does_not_drop_or_block_other_items(self):
        items = [
            {"id": "-1_1", "visual": {"template": "a", "original_photo": "one.jpg", "headline": "h", "detail": "d"}},
            {"id": "-1_2", "visual": {"template": "a", "original_photo": "two.jpg", "headline": "h", "detail": "d"}},
            {"id": "-1_3"},
        ]
        health = {}
        with tempfile.TemporaryDirectory() as directory:
            def builder(**kwargs):
                if kwargs["item_id"] == "-1_1":
                    raise RuntimeError("bad visual")
                output = Path(directory) / "m1_2.png"
                output.write_bytes(b"png")
                return output
            monitor.build_feed_visuals(items, health, builder=builder, output_dir=Path(directory), fingerprint=lambda *_: "fp")
        self.assertEqual([item["id"] for item in items], ["-1_1", "-1_2", "-1_3"])
        self.assertNotIn("visual_url", items[0])
        self.assertEqual(items[1]["visual_url"], f"{monitor.PUBLIC_SITE_BASE}/visuals/m1_2.png")
        self.assertNotIn("visual_url", items[2])
        self.assertEqual(len(health["visual_errors"]), 1)
        self.assertEqual(health["built_visuals"], 1)

    def test_failed_rebuild_removes_stale_visual_url(self):
        item = {
            "id": "-1_1",
            "visual_url": f"{monitor.PUBLIC_SITE_BASE}/visuals/m1_1.png",
            "visual": {"template": "a", "original_photo": "one.jpg", "headline": "h", "detail": "d"},
        }
        with tempfile.TemporaryDirectory() as directory:
            def builder(**kwargs):
                raise RuntimeError("bad visual")
            monitor.build_feed_visuals([item], {}, builder=builder, output_dir=Path(directory), fingerprint=lambda *_: "fp")
        self.assertNotIn("visual_url", item)

    def test_missing_rebuild_output_removes_stale_visual_url(self):
        item = {
            "id": "-1_1",
            "visual_url": f"{monitor.PUBLIC_SITE_BASE}/visuals/m1_1.png",
            "visual": {"template": "a", "original_photo": "one.jpg", "headline": "h", "detail": "d"},
        }
        with tempfile.TemporaryDirectory() as directory:
            monitor.build_feed_visuals(
                [item], {}, builder=lambda **kwargs: Path(directory) / "missing.png",
                output_dir=Path(directory), fingerprint=lambda *_: "fp",
            )
        self.assertNotIn("visual_url", item)

    def test_fingerprint_skips_unchanged_visual_and_rebuilds_changed_text(self):
        item = {
            "id": "-1_9",
            "visual": {"template": "a", "original_photo": "one.jpg", "headline": "first", "detail": "d"},
        }
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            def builder(**kwargs):
                calls.append(kwargs["headline"])
                output = output_dir / "m1_9.png"
                output.write_bytes(b"png")
                return output
            fingerprint = lambda item, request: "|".join((item["id"], request["headline"], request["detail"]))
            monitor.build_feed_visuals([item], {}, builder=builder, output_dir=output_dir, fingerprint=fingerprint)
            monitor.build_feed_visuals([item], {}, builder=builder, output_dir=output_dir, fingerprint=fingerprint)
            item["visual"]["headline"] = "changed"
            monitor.build_feed_visuals([item], {}, builder=builder, output_dir=output_dir, fingerprint=fingerprint)
        self.assertEqual(calls, ["first", "changed"])
        self.assertEqual(item["visual_fingerprint"], "-1_9|changed|d")

    def test_cleanup_removes_only_visuals_outside_current_feed(self):
        items = [
            {"id": "-1_1", "visual": {"template": "a", "original_photo": "1", "headline": "h", "detail": "d"}},
            {"id": "-1_2", "visual": {"template": "a", "original_photo": "2", "headline": "h", "detail": "d"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / ".gitkeep").touch()
            (output_dir / "m1_1.png").write_bytes(b"current")
            (output_dir / "m1_2.png").write_bytes(b"current but failed")
            (output_dir / "m1_3.png").write_bytes(b"expired")
            def builder(**kwargs):
                if kwargs["item_id"] == "-1_2":
                    raise RuntimeError("failed")
                return output_dir / "m1_1.png"
            monitor.build_feed_visuals(items, {}, builder=builder, output_dir=output_dir, fingerprint=lambda *_: "new")
            self.assertTrue((output_dir / "m1_1.png").exists())
            self.assertTrue((output_dir / "m1_2.png").exists())
            self.assertFalse((output_dir / "m1_3.png").exists())
            self.assertTrue((output_dir / ".gitkeep").exists())


if __name__ == "__main__":
    unittest.main()
