import hashlib
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from PIL import Image

import visual_builder

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "MLSPO_Codex_sources_under25MB.zip"
FONT = ROOT / "onest-cyrillic-wght-normal.woff2"
TEMPLATE = "templates/01_glavnoe.svg"


class VisualBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.photo = self.work / "original.png"
        Image.new("RGB", (1400, 900), (13, 177, 91)).save(self.photo)

    def build(self, **overrides):
        args = dict(item_id="-12_34", template=TEMPLATE, original_photo=self.photo,
                    headline="Новый проект студентов", detail="Подробности в публикации",
                    output_dir=self.work / "visuals", archive=ARCHIVE, font=FONT)
        args.update(overrides)
        return visual_builder.build_visual(**args)

    def test_png_is_1080_square_and_named_from_item_id(self):
        output = self.build()
        self.assertEqual(output.name, "m12_34.png")
        with Image.open(output) as image:
            self.assertEqual(image.size, (1080, 1080))
            self.assertEqual(image.format, "PNG")

    def test_prepared_svg_embeds_exact_original_photo(self):
        prepared = visual_builder.prepare_svg(ARCHIVE, TEMPLATE, self.photo,
                                               "Заголовок", "Деталь")
        self.assertIn(self.photo.read_bytes(), visual_builder.embedded_photo_bytes(prepared))

    def test_onest_is_loaded_for_rendering(self):
        with mock.patch.object(visual_builder, "_render_svg") as render:
            render.side_effect = lambda svg, target, font: Image.new("RGB", (1080, 1080)).save(target, format="PNG")
            self.build()
        passed_font = render.call_args.args[2]
        self.assertEqual(passed_font, FONT)
        self.assertEqual(visual_builder.font_family_name(passed_font), "Onest")
        with tempfile.TemporaryDirectory() as directory:
            environment = visual_builder._fontconfig(passed_font, directory)
            selected = subprocess.run(
                ["fc-match", "--format", "%{file}", "Onest"], env=environment,
                check=True, capture_output=True, text=True,
            ).stdout
        self.assertEqual(Path(selected).resolve(), FONT.resolve())

    def test_editorial_punctuation_uses_real_fallback_glyphs(self):
        for text in (
            "Студент СПТ — второй в России",
            "«Профессионалы» • тестирование игрового ПО",
        ):
            prepared = visual_builder.prepare_svg(ARCHIVE, TEMPLATE, self.photo, text, text)
            root = ET.fromstring(prepared)
            for element_id in ("HEADLINE_TEXT", "DETAIL_TEXT"):
                element = root.find(f".//*[@id='{element_id}']")
                lines = []
                for tspan in list(element):
                    if tspan.get("x") is not None:
                        lines.append(tspan.text or "")
                    else:
                        lines[-1] += tspan.text or ""
                rendered_text = " ".join(lines)
                self.assertEqual(rendered_text, text)
        families = visual_builder.font_families_for_text("—–«»•", FONT)
        self.assertTrue(all(family != "Onest" for family in families))

    def test_wide_text_is_rejected_by_measured_width(self):
        with self.assertRaisesRegex(visual_builder.TextOverflowError, "HEADLINE_TEXT"):
            self.build(headline="Ш" * 31)

    def test_source_svg_is_unchanged_and_only_allowed_nodes_change(self):
        before_archive = hashlib.sha256(ARCHIVE.read_bytes()).digest()
        with zipfile.ZipFile(ARCHIVE) as zf:
            original = zf.read(TEMPLATE)
        prepared = visual_builder.prepare_svg(ARCHIVE, TEMPLATE, self.photo, "Заголовок", "Деталь")
        self.assertEqual(hashlib.sha256(ARCHIVE.read_bytes()).digest(), before_archive)
        visual_builder.assert_only_allowed_changes(original, prepared)

    def test_photo_hint_is_absent_from_prepared_svg(self):
        prepared = visual_builder.prepare_svg(ARCHIVE, TEMPLATE, self.photo, "Заголовок", "Деталь")
        ids = {node.get("id") for node in ET.fromstring(prepared).iter()}
        self.assertNotIn("PHOTO_HINT", ids)
        self.assertNotIn(b"PHOTO_HINT", prepared)

    def test_too_long_text_is_controlled_error(self):
        started = time.monotonic()
        with self.assertRaisesRegex(visual_builder.TextOverflowError, "HEADLINE_TEXT"):
            self.build(headline="очень " * 100)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse((self.work / "visuals" / "m12_34.png").exists())

    def test_broken_photo_leaves_no_partial_png(self):
        broken = self.work / "broken.jpg"
        broken.write_bytes(b"not an image")
        with self.assertRaises(visual_builder.InvalidPhotoError):
            self.build(original_photo=broken)
        self.assertFalse((self.work / "visuals" / "m12_34.png").exists())


if __name__ == "__main__":
    unittest.main()
