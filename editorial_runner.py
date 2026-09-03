#!/usr/bin/env python3
"""Run the VK monitor with the human-authored MLSPO editorial queue applied."""

import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import monitor

EDITORIAL_QUEUE_FILE = monitor.ROOT / "editorial_queue.json"
EDITORIAL_STATUSES = {"new", "ready", "sent", "skip"}


def load_editorial_queue(path, health):
    """Load the editorial queue without allowing a bad file to stop monitoring."""
    errors = health.setdefault("editorial_errors", [])
    health["editorial_queue_loaded"] = False
    try:
        queue = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(queue, dict):
            raise ValueError("Корень editorial_queue.json должен быть объектом")
    except FileNotFoundError:
        errors.append({"error": "editorial_queue.json не найден"})
        queue = {}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append({"error": f"Не удалось загрузить editorial_queue.json: {exc}"})
        queue = {}
    else:
        health["editorial_queue_loaded"] = True

    health["editorial_items_total"] = len(queue)
    health["editorial_ready_items"] = sum(
        isinstance(entry, dict) and entry.get("status") == "ready"
        for entry in queue.values()
    )
    health["editorial_sent_items"] = sum(
        isinstance(entry, dict) and entry.get("status") == "sent"
        for entry in queue.values()
    )
    health["editorial_skipped_items"] = sum(
        isinstance(entry, dict) and entry.get("status") == "skip"
        for entry in queue.values()
    )
    return queue


def original_photo_path(item, image_dir=None):
    """Return the first mirrored original photo as a local builder input."""
    image_dir = Path(image_dir or monitor.PUBLIC_IMAGE_DIR)
    for attachment in item.get("attachments") or []:
        if not isinstance(attachment, dict) or attachment.get("type") != "photo":
            continue
        local_path = attachment.get("local_path")
        if local_path and Path(local_path).is_file():
            return str(local_path)
        mirror_url = attachment.get("mirror_url")
        if mirror_url:
            candidate = image_dir / Path(urlparse(mirror_url).path).name
            if candidate.is_file():
                try:
                    return str(candidate.relative_to(monitor.ROOT))
                except ValueError:
                    return str(candidate)
        # Only the first original photo is eligible; do not silently use a later one.
        return None
    return None


def _clear_editorial_fields(item):
    for field in (
        "editorial_status",
        "rewrite",
        "visual",
        "visual_url",
        "visual_fingerprint",
    ):
        item.pop(field, None)


def apply_editorial_queue(items, queue, health, image_dir=None):
    """Attach public editorial fields and ready-to-build visual requests to feed items."""
    errors = health.setdefault("editorial_errors", [])
    queue_loaded = health.get("editorial_queue_loaded", True)

    for item in items:
        item_id = str(item.get("id"))
        if item_id not in queue:
            if queue_loaded:
                _clear_editorial_fields(item)
            continue

        entry = queue[item_id]
        if not isinstance(entry, dict):
            errors.append({"item_id": item_id, "error": "Запись очереди должна быть объектом"})
            if queue_loaded:
                _clear_editorial_fields(item)
            continue

        status = entry.get("status")
        if status not in EDITORIAL_STATUSES:
            errors.append({"item_id": item_id, "error": f"Недопустимый status: {status!r}"})
            if queue_loaded:
                _clear_editorial_fields(item)
            continue

        rewrite = entry.get("rewrite")
        if "rewrite" not in entry and status != "ready":
            errors.append({"item_id": item_id, "error": "Нет обязательного поля rewrite"})

        item["editorial_status"] = status
        if rewrite:
            item["rewrite"] = rewrite
        else:
            item.pop("rewrite", None)

        if status != "ready":
            if status in {"new", "skip"}:
                item.pop("visual", None)
                item.pop("visual_url", None)
                item.pop("visual_fingerprint", None)
            continue

        missing = []
        if not isinstance(rewrite, str) or not rewrite.strip():
            missing.append("rewrite")
        missing.extend(
            name for name in ("template", "headline", "detail") if not entry.get(name)
        )
        photo = original_photo_path(item, image_dir=image_dir)
        if not photo:
            missing.append("original_photo")
        if missing:
            item.pop("visual", None)
            item.pop("visual_url", None)
            item.pop("visual_fingerprint", None)
            errors.append(
                {"item_id": item_id, "error": "Нет данных для визуала: " + ", ".join(missing)}
            )
            continue

        item["visual"] = {
            "template": entry["template"],
            "headline": entry["headline"],
            "detail": entry["detail"],
            "original_photo": photo,
        }


def mirror_feed_photos_with_editorial(items, session, health):
    """Mirror feed photos, forcing the first original photo for ready editorial items."""
    queue = load_editorial_queue(EDITORIAL_QUEUE_FILE, health)
    ready_item_ids = {
        str(item_id)
        for item_id, entry in queue.items()
        if isinstance(entry, dict) and entry.get("status") == "ready"
    }

    if monitor.IMAGE_UPLOAD_DIR.exists():
        shutil.rmtree(monitor.IMAGE_UPLOAD_DIR)
    monitor.IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    monitor.PUBLIC_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    seen_texts = set()
    mirrored = 0
    published = 0
    errors = []
    needed_files = set()

    for item in items:
        photos = [
            a
            for a in (item.get("attachments") or [])
            if a.get("type") == "photo" and a.get("url")
        ]
        if not photos:
            continue

        text_key = re.sub(
            r"\s+",
            " ",
            (item.get("text") or item.get("repost_text") or "").strip(),
        ).lower()
        is_duplicate_text = bool(text_key) and text_key in seen_texts
        if is_duplicate_text and str(item.get("id")) not in ready_item_ids:
            continue
        if text_key:
            seen_texts.add(text_key)

        done = 0
        for idx, attachment in enumerate(photos, start=1):
            if done >= monitor.MAX_MIRRORED_PHOTOS_PER_POST:
                break

            original_url = attachment["url"]
            filename = (
                f"{monitor.safe_post_key(item.get('id'))}_{idx}"
                f"{monitor.image_extension(original_url)}"
            )
            public_target = monitor.PUBLIC_IMAGE_DIR / filename
            upload_target = monitor.IMAGE_UPLOAD_DIR / filename
            needed_files.add(filename)

            try:
                if not public_target.exists():
                    monitor.download_original_photo(session, original_url, public_target)
                    shutil.copy2(public_target, upload_target)
                    mirrored += 1

                attachment["mirror_url"] = monitor.pages_asset_url(filename)
                attachment["release_url"] = monitor.release_asset_url(filename)
                published += 1
                done += 1
            except Exception as exc:
                errors.append(
                    {
                        "post_url": item.get("post_url"),
                        "photo_url": original_url,
                        "error": str(exc)[:300],
                    }
                )

    for path in monitor.PUBLIC_IMAGE_DIR.iterdir():
        if path.is_file() and path.name not in needed_files:
            path.unlink()

    health["mirrored_photos"] = mirrored
    health["published_photos"] = published
    health["image_errors"] = errors[:100]

    apply_editorial_queue(items, queue, health, image_dir=monitor.PUBLIC_IMAGE_DIR)


def main():
    # Preserve the already-tested feed pipeline. The editorial layer is applied at
    # the existing photo-mirroring hook, immediately before build_feed_visuals().
    monitor.mirror_feed_photos = mirror_feed_photos_with_editorial
    monitor.main()


if __name__ == "__main__":
    main()
