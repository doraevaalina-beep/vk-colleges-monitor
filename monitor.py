#!/usr/bin/env python3
import os
import re
import json
import time
import html
from pathlib import Path
from datetime import datetime, timezone, timedelta
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

# VK with a service token is conservative at ~3 calls/sec.
REQUEST_DELAY = 0.42
POSTS_PER_SOURCE = 20
KEEP_HOURS = 72
MAX_ITEMS = 1000

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
    best = max(sizes, key=lambda x: (x.get("width", 0) * x.get("height", 0), x.get("width", 0)))
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
            if a.get("url"):
                desc_parts.append(f'<br><a href="{html.escape(a["url"])}">{html.escape(a.get("type","attachment"))}</a>')
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
        "note": "Ошибки здесь не содержат VK-токен."
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

    save_json(STATE_FILE, state)
    save_json(FEED_JSON, {"generated_at": iso(), "items": kept})
    save_json(HEALTH_JSON, health)
    write_rss(kept)

    print(f"Источников: {len(sources)}; успешно: {len(health['ok'])}; ошибок: {len(health['errors'])}; новых постов: {health['new_posts']}")

if __name__ == "__main__":
    main()
