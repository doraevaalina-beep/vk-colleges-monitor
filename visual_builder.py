#!/usr/bin/env python3
"""Build 1080x1080 MLSPO cards from the original SVG source bundle."""

import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent
DEFAULT_ARCHIVE = ROOT / "MLSPO_Codex_sources_under25MB.zip"
DEFAULT_FONT = ROOT / "onest-cyrillic-wght-normal.woff2"
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "visuals"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ALLOWED_IDS = {"PHOTO_REPLACE", "HEADLINE_TEXT", "DETAIL_TEXT", "PHOTO_HINT"}


class VisualBuildError(RuntimeError):
    """A controlled error which must not abort feed processing."""


class TextOverflowError(VisualBuildError):
    pass


class InvalidPhotoError(VisualBuildError):
    pass


class FontLoadError(VisualBuildError):
    pass


def safe_item_key(item_id):
    value = str(item_id or "post")
    if value.startswith("-"):
        value = "m" + value[1:]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _element_by_id(root, element_id):
    matches = [node for node in root.iter() if node.get("id") == element_id]
    if len(matches) != 1:
        raise VisualBuildError(f"В шаблоне должен быть ровно один {element_id}")
    return matches[0]


def _validated_photo(path):
    path = Path(path)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        raise InvalidPhotoError(f"Повреждённое или неподдерживаемое фото: {path}") from exc
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        mime = "image/png" if path.read_bytes().startswith(b"\x89PNG") else "image/jpeg"
    return path.read_bytes(), mime


def font_family_name(font):
    result = subprocess.run(["fc-scan", "--format", "%{family}\n", str(font)],
                            check=True, capture_output=True, text=True)
    return result.stdout.splitlines()[0].split(",", 1)[0].strip()


def _fontconfig(font, directory):
    font = Path(font).resolve()
    if not font.is_file() or font_family_name(font) != "Onest":
        raise FontLoadError(f"Фирменный шрифт Onest не найден: {font}")
    config = Path(directory) / "fonts.conf"
    config.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
        '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
        f'<dir>{font.parent}</dir><cachedir>{directory}</cachedir></fontconfig>',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = str(config)
    env["FONTCONFIG_PATH"] = str(Path(directory))
    match = subprocess.run(["fc-match", "--format", "%{file}", "Onest"], env=env,
                           check=True, capture_output=True, text=True).stdout
    if Path(match).resolve() != font:
        raise FontLoadError(f"Рендерер не выбрал переданный Onest: {match}")
    return env


def _families_with_env(text, env):
    families = {}
    for character in dict.fromkeys(text):
        if character.isspace():
            families[character] = "Onest"
            continue
        pattern = f"Onest:charset={ord(character):04x}"
        result = subprocess.run(
            ["fc-match", "--format", "%{family[0]}", pattern], env=env,
            check=True, capture_output=True, text=True,
        )
        family = result.stdout.strip()
        if not family:
            raise FontLoadError(f"Не найден глиф U+{ord(character):04X}")
        families[character] = family
    return [families[character] for character in text]


def font_families_for_text(text, font=DEFAULT_FONT):
    """Return the Fontconfig-selected family for every character."""
    with tempfile.TemporaryDirectory(prefix="mlspo-font-") as temporary:
        return _families_with_env(text, _fontconfig(font, temporary))


WRAP_PROGRAM = r"""
import json
import sys
import cairocffi as cairo

payload = json.load(sys.stdin)
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
context = cairo.Context(surface)
weight = cairo.FONT_WEIGHT_BOLD if payload["weight"] >= 600 else cairo.FONT_WEIGHT_NORMAL

def width(value):
    total = 0
    runs = []
    for character in value:
        family = payload["families"][character]
        if runs and runs[-1][1] == family:
            runs[-1][0] += character
        else:
            runs.append([character, family])
    for text, family in runs:
        context.select_font_face(family, cairo.FONT_SLANT_NORMAL, weight)
        context.set_font_size(payload["size"])
        total += context.text_extents(text)[4]
    return total

lines = []
for word in payload["text"].split(" "):
    if width(word) > payload["max_width"]:
        print(json.dumps({"error": "word"}))
        break
    candidate = f"{lines[-1]} {word}" if lines else word
    if lines and width(candidate) > payload["max_width"]:
        lines.append(word)
        if len(lines) > payload["max_lines"]:
            print(json.dumps({"error": "lines", "count": len(lines)}))
            break
    elif lines:
        lines[-1] = candidate
    else:
        lines.append(word)
else:
    print(json.dumps({"lines": lines}, ensure_ascii=False))
"""


def _wrap(text, max_width, max_lines, element_id, size, weight, env):
    normalized = " ".join(str(text).split())
    if not normalized:
        raise TextOverflowError(f"{element_id}: текст не может быть пустым")
    family_map = dict(zip(normalized, _families_with_env(normalized, env)))
    payload = {
        "text": normalized,
        "families": family_map,
        "size": size,
        "weight": weight,
        "max_width": max_width,
        "max_lines": max_lines,
    }
    result = subprocess.run(
        [sys.executable, "-c", WRAP_PROGRAM], input=json.dumps(payload), env=env,
        check=True, capture_output=True, text=True,
    )
    wrapped = json.loads(result.stdout)
    if wrapped.get("error") == "word":
        raise TextOverflowError(f"{element_id}: слово не помещается по ширине")
    if wrapped.get("error") == "lines":
        raise TextOverflowError(
            f"{element_id}: текст не помещается ({wrapped['count']} строк)"
        )
    lines = wrapped["lines"]
    return [(line, [family_map[c] for c in line]) for line in lines]


def _replace_text(node, lines):
    for child in list(node):
        node.remove(child)
    node.text = None
    x = node.get("x", "64")
    for index, (line, families) in enumerate(lines):
        runs = []
        for character, family in zip(line, families):
            if runs and runs[-1][1] == family:
                runs[-1][0] += character
            else:
                runs.append([character, family])
        for run_index, (run, family) in enumerate(runs):
            attributes = {}
            if run_index == 0:
                attributes = {"x": x, "dy": "0" if index == 0 else "60"}
            if family != "Onest":
                attributes["font-family"] = family
            tspan = ET.SubElement(node, f"{{{SVG_NS}}}tspan", attributes)
            tspan.text = run


def prepare_svg(archive, template, original_photo, headline, detail, font=DEFAULT_FONT):
    """Return a modified in-memory SVG; neither archive nor its SVG is written."""
    archive = Path(archive)
    template = str(template)
    if not template.startswith("templates/") or not template.endswith(".svg") or ".." in Path(template).parts:
        raise VisualBuildError("Разрешены только оригинальные SVG из каталога templates архива")
    try:
        with zipfile.ZipFile(archive) as bundle:
            source = bundle.read(template)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise VisualBuildError(f"Не удалось прочитать шаблон {template}") from exc

    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    root = ET.fromstring(source)
    photo_bytes, mime = _validated_photo(original_photo)
    photo = _element_by_id(root, "PHOTO_REPLACE")
    images = [node for node in photo.iter() if node.tag == f"{{{SVG_NS}}}image"]
    if len(images) != 1:
        raise VisualBuildError("PHOTO_REPLACE должен содержать ровно одно изображение")
    images[0].set(f"{{{XLINK_NS}}}href", f"data:{mime};base64," + base64.b64encode(photo_bytes).decode("ascii"))

    with tempfile.TemporaryDirectory(prefix="mlspo-font-") as temporary:
        env = _fontconfig(font, temporary)
        headline_node = _element_by_id(root, "HEADLINE_TEXT")
        headline_lines = _wrap(
            headline, 952, 2, "HEADLINE_TEXT",
            float(headline_node.get("font-size", 56)),
            int(headline_node.get("font-weight", 400)), env,
        )
        _replace_text(headline_node, headline_lines)
        detail_node = _element_by_id(root, "DETAIL_TEXT")
        detail_lines = _wrap(detail, 582, 1, "DETAIL_TEXT", float(detail_node.get("font-size", 24)), int(detail_node.get("font-weight", 400)), env)
        _replace_text(detail_node, detail_lines)

    hint = _element_by_id(root, "PHOTO_HINT")
    for parent in root.iter():
        if hint in list(parent):
            parent.remove(hint)
            break
    result = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    assert_only_allowed_changes(source, result)
    return result


def embedded_photo_bytes(svg):
    root = ET.fromstring(svg)
    photo = _element_by_id(root, "PHOTO_REPLACE")
    values = []
    for node in photo.iter():
        href = node.get(f"{{{XLINK_NS}}}href", "")
        if href.startswith("data:image/"):
            values.append(base64.b64decode(href.split(",", 1)[1]))
    return values


def _normalized_outside_allowed(svg):
    root = ET.fromstring(svg)
    for parent in root.iter():
        for child in list(parent):
            if child.get("id") in ALLOWED_IDS:
                parent.remove(child)
    return ET.tostring(root, encoding="utf-8")


def assert_only_allowed_changes(original, prepared):
    """Guard against accidental edits to brand geometry, colors or typography."""
    if _normalized_outside_allowed(original) != _normalized_outside_allowed(prepared):
        raise VisualBuildError("Обнаружено изменение вне разрешённых элементов SVG")


def _render_svg(svg, target, font):
    with tempfile.TemporaryDirectory(prefix="mlspo-font-") as temporary:
        env = _fontconfig(font, temporary)
        command = [sys.executable, "-c", (
            "import sys,cairosvg; cairosvg.svg2png(bytestring=sys.stdin.buffer.read(),"
            "write_to=sys.argv[1],output_width=1080,output_height=1080)"
        ), str(target)]
        try:
            subprocess.run(command, input=svg, env=env, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"").decode("utf-8", "replace")[-500:]
            raise VisualBuildError(f"Ошибка SVG-рендерера: {detail or exc}") from exc


def build_visual(*, item_id, template, original_photo, headline, detail,
                 output_dir=DEFAULT_OUTPUT_DIR, archive=DEFAULT_ARCHIVE, font=DEFAULT_FONT):
    """Atomically build one PNG named after item_id and return its Path."""
    svg = prepare_svg(archive, template, original_photo, headline, detail, font)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{safe_item_key(item_id)}.png"
    temporary = output_dir / f".{target.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    try:
        _render_svg(svg, temporary, Path(font))
        from PIL import Image
        with Image.open(temporary) as image:
            if image.format != "PNG" or image.size != (1080, 1080):
                raise VisualBuildError("Рендерер вернул не PNG 1080×1080")
            image.verify()
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--template", required=True, help="templates/*.svg внутри исходного ZIP")
    parser.add_argument("--original-photo", type=Path, required=True)
    parser.add_argument("--headline", required=True)
    parser.add_argument("--detail", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    args = parser.parse_args(argv)
    output = build_visual(item_id=args.item_id, template=args.template,
                          original_photo=args.original_photo, headline=args.headline,
                          detail=args.detail, output_dir=args.output_dir,
                          archive=args.archive, font=args.font)
    print(json.dumps({"visual": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
