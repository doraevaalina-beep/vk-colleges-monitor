#!/usr/bin/env python3
import os
import re
import json
import hashlib
import time
import html
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from xml.etree.ElementTree import Element, SubElement, ElementTree

import requests

API_VERSION = "5.199"
API_URL = "https://api.vk.com/method/wall.get"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
SOURCES_FILE = ROOT / "sources.txt"
DOCS = ROOT / "docs"
FEED_JSON = DOCS / "feed.json"
FEED_XML = DOCS / "feed.xml"
HEALTH_JSON = DOCS / "health.json"
IMAGE_UPLOAD_DIR = ROOT / ".image_uploads"
PUBLIC_IMAGE_DIR = DOCS / "images"
PUBLIC_VISUAL_DIR = DOCS / "visuals"
VISUAL_ARCHIVE = ROOT / "MLSPO_Codex_sources_under25MB.zip"
VISUAL_FONT = ROOT / "onest-cyrillic-wght-normal.woff2"
VISUAL_BUILDER_SOURCE = ROOT / "visual_builder.py"
PUBLIC_SITE_BASE = os.environ.get(
    "PUBLIC_SITE_BASE",
    "https://doraevaalina-beep.github.io/vk-colleges-monitor",
).rstrip("/")

# Public GitHub Release used only as a rolling image mirror.
RELEASE_TAG = "vk-images"
DEFAULT_REPO = "doraevaalina-beep/vk-colleges-monitor"

# VK with a service token is conservative at ~3 calls/sec.
REQUEST_DELAY = 0.42
POSTS_PER_SOURCE = 20
KEEP_HOURS = 72
MAX_ITEMS = 1000
# One original VK photo is enough for the visual card preview.
MAX_MIRRORED_PHOTOS_PER_POST = 1

def utcnow():
    return datetime.now(timezone.utc)

def iso(ts=None):
    dt = utcnow() if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def normalize_source(raw):
    raw = raw.strip()
    raw = re.sub(r"^https?://", "", raw, flags=re.I)
    raw = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if raw.lower().startswith("www."):
        raw = raw[4:]
    if not raw.lower().startswith("vk.com/"):
        raise ValueError(f"Не VK URL: {raw}")
    slug = raw.split("/", 1)[1]
    if not slug:
        raise ValueError(f"Пустой адрес сообщества: {raw}")
    return slug

def wall_params(slug):
    m = re.fullmatch(r"(?:club|public|event)(\d+)", slug, flags=re.I)
    params = {
        "v": API_VERSION,
        "count": POSTS_PER_SOURCE,
        "filter": "owner",
        "extended": 1,
    }
    if m:
        params["owner_id"] = -int(m.group(1))
    else:
        params["domain"] = slug
    return params

def vk_request(session, token, slug):
    headers = {"Authorization": f"Bearer {token}"}
    r = session.get(API_URL, params=wall_params(slug), headers=headers, timeout=20)
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        err = payload["error"]
        raise RuntimeError(f"VK error {err.get('error_code')}: {err.get('error_msg')}")
    return payload["response"]

def community_name(response, fallback):
    groups = response.get("groups") or []
    if groups:
        return groups[0].get("name") or fallback
    return fallback

def best_photo_url(photo):
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    # Prefer a good-quality image without needlessly taking the absolute largest file.
    suitable = [x for x in sizes if 900 <= max(x.get("width", 0), x.get("height", 0)) <= 1600]
    pool = suitable or sizes
    best = max(pool, key=lambda x: (x.get("width", 0) * x.get("height", 0), x.get("width", 0)))
    return best.get("url")

def simplify_attachments(attachments):
    out = []
    for a in attachments or []:
        t = a.get("type")
        obj = a.get(t, {}) if t else {}
        if t == "photo":
            url = best_photo_url(obj)
            if url:
                out.append({"type": "photo", "url": url})
        elif t == "video":
            oid, vid = obj.get("owner_id"), obj.get("id")
            if oid is not None and vid is not None:
                out.append({
                    "type": "video",
                    "title": obj.get("title"),
                    "url": f"https://vk.com/video{oid}_{vid}"
                })
        elif t == "link":
            if obj.get("url"):
                out.append({"type": "link", "title": obj.get("title"), "url": obj.get("url")})
        elif t == "doc":
            if obj.get("url"):
                out.append({"type": "doc", "title": obj.get("title"), "url": obj.get("url")})
        elif t == "audio":
            out.append({
                "type": "audio",
                "artist": obj.get("artist"),
                "title": obj.get("title")
            })
    return out

def repost_text(post):
    parts = []
    for cp in post.get("copy_history") or []:
        txt = (cp.get("text") or "").strip()
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)

def make_item(source_url, name, post, discovered_at):
    owner_id = post.get("owner_id")
    post_id = post.get("id")
    url = f"https://vk.com/wall{owner_id}_{post_id}"
    text = (post.get("text") or "").strip()
    rp = repost_text(post)
    return {
        "id": f"{owner_id}_{post_id}",
        "community": name,
        "source_url": source_url,
        "post_url": url,
        "published_at": iso(post.get("date", 0)),
        "discovered_at": discovered_at,
        "text": text,
        "repost_text": rp,
        "attachments": simplify_attachments(post.get("attachments")),
    }

def safe_post_key(item_id):
    # Example: -174792280_8201 -> m174792280_8201
    value = str(item_id or "post")
    if value.startswith("-"):
        value = "m" + value[1:]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)

def image_extension(url):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"

def release_asset_url(filename):
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or DEFAULT_REPO
    return f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{filename}"

def pages_asset_url(filename):
    return f"{PUBLIC_SITE_BASE}/images/{filename}"

def pages_visual_url(filename):
    return f"{PUBLIC_SITE_BASE}/visuals/{filename}"

def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def visual_fingerprint(item, request):
    photo = Path(request["original_photo"])
    if not photo.is_absolute():
        photo = ROOT / photo
    inputs = {
        "id": str(item.get("id")),
        "template": request["template"],
        "headline": request["headline"],
        "detail": request["detail"],
        "photo_sha256": _file_sha256(photo),
        "archive_sha256": _file_sha256(VISUAL_ARCHIVE),
        "font_sha256": _file_sha256(VISUAL_FONT),
        "builder_sha256": _file_sha256(VISUAL_BUILDER_SOURCE),
    }
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def build_feed_visuals(items, health, builder=None, output_dir=PUBLIC_VISUAL_DIR,
                       fingerprint=visual_fingerprint):
    """Build explicitly configured cards without allowing one failure to stop the feed."""
    if builder is None:
        from visual_builder import build_visual
        builder = build_visual
    output_dir = Path(output_dir)
    errors = []
    built = 0
    built_files = []
    reused = 0
    needed_files = set()
    for item in items:
        request = item.get("visual")
        if not isinstance(request, dict):
            continue
        item.pop("visual_url", None)
        filename = f"{safe_post_key(item.get('id'))}.png"
        needed_files.add(filename)
        expected_output = output_dir / filename
        required = ("template", "original_photo", "headline", "detail")
        missing = [name for name in required if not request.get(name)]
        if missing:
            item.pop("visual_fingerprint", None)
            errors.append({"item_id": item.get("id"), "error": "Нет полей: " + ", ".join(missing)})
            continue
        try:
            current_fingerprint = fingerprint(item, request)
            if item.get("visual_fingerprint") == current_fingerprint and expected_output.is_file():
                item["visual_url"] = pages_visual_url(filename)
                reused += 1
                continue
            output = builder(
                item_id=item.get("id"),
                template=request["template"],
                original_photo=request["original_photo"],
                headline=request["headline"],
                detail=request["detail"],
                output_dir=output_dir,
            )
            # Publish the URL only after the builder returned an existing PNG.
            output = Path(output)
            if not output.is_file():
                raise RuntimeError("Сборщик не создал PNG")
            item["visual_url"] = pages_visual_url(output.name)
            item["visual_fingerprint"] = current_fingerprint
            built += 1
            built_files.append(output.name)
        except Exception as exc:
            item.pop("visual_fingerprint", None)
            errors.append({"item_id": item.get("id"), "error": str(exc)[:500]})
    deleted = 0
    if output_dir.exists():
        for output in output_dir.glob("*.png"):
            if output.name not in needed_files:
                output.unlink()
                deleted += 1
    health["built_visuals"] = built
    health["built_visual_files"] = built_files
    health["reused_visuals"] = reused
    health["deleted_visuals"] = deleted
    health["visual_errors"] = errors[:100]

def download_original_photo(session, source_url, target_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; VKFeedMonitor/1.0)",
        "Referer": "https://vk.com/",
    }
    r = session.get(source_url, headers=headers, timeout=30)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if not (ctype.startswith("image/") or source_url.lower().split("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp"))):
        raise RuntimeError(f"Неожиданный Content-Type: {ctype or 'unknown'}")
    target_path.write_bytes(r.content)

def mirror_feed_photos(items, session, health):
    """
    Publish one ORIGINAL VK photo for each unique-text post to GitHub Pages.
    The same file is also staged for the rolling GitHub Release as a backup.
    Existing release assets restored into docs/images by the workflow are reused.
    """
    if IMAGE_UPLOAD_DIR.exists():
        shutil.rmtree(IMAGE_UPLOAD_DIR)
    IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    seen_texts = set()
    mirrored = 0
    published = 0
    errors = []
    needed_files = set()

    for item in items:
        photos = [a for a in (item.get("attachments") or []) if a.get("type") == "photo" and a.get("url")]
        if not photos:
            continue

        text_key = re.sub(r"\s+", " ", (item.get("text") or item.get("repost_text") or "").strip()).lower()
        is_duplicate_text = bool(text_key) and text_key in seen_texts
        if is_duplicate_text:
            continue
        if text_key:
            seen_texts.add(text_key)

        done = 0
        for idx, attachment in enumerate(photos, start=1):
            if done >= MAX_MIRRORED_PHOTOS_PER_POST:
                break

            original_url = attachment["url"]
            filename = f"{safe_post_key(item.get('id'))}_{idx}{image_extension(original_url)}"
            public_target = PUBLIC_IMAGE_DIR / filename
            upload_target = IMAGE_UPLOAD_DIR / filename
            needed_files.add(filename)

            try:
                # The workflow restores existing release assets into docs/images first,
                # so old feed items do not need to be downloaded from VK every hour.
                if not public_target.exists():
                    download_original_photo(session, original_url, public_target)
                    shutil.copy2(public_target, upload_target)
                    mirrored += 1

                attachment["mirror_url"] = pages_asset_url(filename)
                attachment["release_url"] = release_asset_url(filename)
                published += 1
                done += 1
            except Exception as e:
                errors.append({
                    "post_url": item.get("post_url"),
                    "photo_url": original_url,
                    "error": str(e)[:300],
                })

    # docs/images is restored from the rolling release on every run. Remove assets
    # that are no longer referenced by the current 72-hour feed before publishing Pages.
    for path in PUBLIC_IMAGE_DIR.iterdir():
        if path.is_file() and path.name not in needed_files:
            path.unlink()

    health["mirrored_photos"] = mirrored
    health["published_photos"] = published
    health["image_errors"] = errors[:100]

def write_rss(items):
    rss = Element("rss", {"version": "2.0"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "VK monitor"
    SubElement(channel, "link").text = "https://vk.com/"
    SubElement(channel, "description").text = "Новые посты отслеживаемых сообществ VK"
    SubElement(channel, "lastBuildDate").text = utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    for it in items:
        node = SubElement(channel, "item")
        text = it.get("text") or it.get("repost_text") or "(публикация без текстовой подписи)"
        title = re.sub(r"\s+", " ", text).strip()[:120]
        SubElement(node, "title").text = f"{it['community']}: {title}"
        SubElement(node, "link").text = it["post_url"]
        SubElement(node, "guid", {"isPermaLink": "true"}).text = it["post_url"]
        pub = datetime.fromisoformat(it["published_at"].replace("Z", "+00:00"))
        SubElement(node, "pubDate").text = pub.strftime("%a, %d %b %Y %H:%M:%S GMT")
        desc_parts = []
        if it.get("text"):
            desc_parts.append(html.escape(it["text"]).replace("\n", "<br>"))
        if it.get("repost_text"):
            desc_parts.append("<hr><b>Репост:</b><br>" + html.escape(it["repost_text"]).replace("\n", "<br>"))
        for a in it.get("attachments") or []:
            attachment_url = a.get("mirror_url") or a.get("url")
            if attachment_url:
                desc_parts.append(f'<br><a href="{html.escape(attachment_url)}">{html.escape(a.get("type","attachment"))}</a>')
        SubElement(node, "description").text = "".join(desc_parts)

    ElementTree(rss).write(FEED_XML, encoding="utf-8", xml_declaration=True)

def main():
    token = os.environ.get("VK_SERVICE_TOKEN", "").strip()
    if not token:
        raise SystemExit("VK_SERVICE_TOKEN не задан")

    sources = []
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        slug = normalize_source(line)
        sources.append((line, slug))

    state = load_json(STATE_FILE, {"sources": {}})
    existing = load_json(FEED_JSON, {"generated_at": None, "items": []})
    feed_items = existing.get("items") or []
    known_urls = {x.get("post_url") for x in feed_items}

    health = {
        "checked_at": iso(),
        "api_version": API_VERSION,
        "source_count": len(sources),
        "ok": [],
        "errors": [],
        "new_posts": 0,
        "mirrored_photos": 0,
        "published_photos": 0,
        "image_errors": [],
        "built_visuals": 0,
        "built_visual_files": [],
        "reused_visuals": 0,
        "deleted_visuals": 0,
        "visual_errors": [],
        "note": "Ошибки здесь не содержат VK-токен. mirror_url ведёт на фото в GitHub Pages; release_url — резервная копия в GitHub Release."
    }

    session = requests.Session()
    discovered_at = iso()

    for idx, (source_url, slug) in enumerate(sources, start=1):
        try:
            resp = vk_request(session, token, slug)
            posts = resp.get("items") or []
            name = community_name(resp, slug)
            st = state["sources"].get(slug, {})
            old_max = st.get("max_post_id")

            ids = [p.get("id") for p in posts if isinstance(p.get("id"), int)]
            current_max = max(ids) if ids else old_max

            # First successful run for a source establishes a baseline.
            # Existing posts are NOT emitted as "new".
            if old_max is None:
                new_posts = []
            else:
                new_posts = [p for p in posts if isinstance(p.get("id"), int) and p["id"] > old_max]
                new_posts.sort(key=lambda p: (p.get("date", 0), p.get("id", 0)))

            for p in new_posts:
                item = make_item(source_url, name, p, discovered_at)
                if item["post_url"] not in known_urls:
                    feed_items.append(item)
                    known_urls.add(item["post_url"])
                    health["new_posts"] += 1

            if current_max is not None:
                state["sources"][slug] = {
                    "source_url": source_url,
                    "community": name,
                    "max_post_id": max(current_max, old_max or 0),
                    "last_ok_at": discovered_at,
                }

            health["ok"].append({
                "source": source_url,
                "community": name,
                "latest_post_id": current_max,
                "fetched": len(posts)
            })

        except Exception as e:
            health["errors"].append({"source": source_url, "error": str(e)[:500]})

        if idx != len(sources):
            time.sleep(REQUEST_DELAY)

    # Keep the public feed compact: only recently discovered posts.
    cutoff = utcnow() - timedelta(hours=KEEP_HOURS)
    kept = []
    for it in feed_items:
        try:
            dt = datetime.fromisoformat(it["discovered_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= cutoff:
            kept.append(it)
    kept.sort(key=lambda x: (x.get("published_at", ""), x.get("post_url", "")), reverse=True)
    kept = kept[:MAX_ITEMS]

    # Backfill current feed as well as future posts. This means the posts already
    # collected in the last 72 hours will receive mirror_url on the first new run.
    mirror_feed_photos(kept, session, health)
    build_feed_visuals(kept, health)

    save_json(STATE_FILE, state)
    save_json(FEED_JSON, {"generated_at": iso(), "items": kept})
    save_json(HEALTH_JSON, health)
    write_rss(kept)

    print(
        f"Источников: {len(sources)}; успешно: {len(health['ok'])}; "
        f"ошибок: {len(health['errors'])}; новых постов: {health['new_posts']}; "
        f"новых зеркал фото: {health['mirrored_photos']}; ошибок фото: {len(health['image_errors'])}"
    )

if __name__ == "__main__":
    main()
