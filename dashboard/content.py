"""
content.py — page CONTENT (images, reviews) for a PDP, read from the Zeus cache.

The analysis snapshot holds scores and findings, but not the page's actual assets.
Zeus already caches them on disk (outputs/zeus_cache/{page_id}.json), so the dashboard
reads them straight from there — no scrape, no API cost — and shows the evidence next
to the verdict.

Results are memoised per process; the cache files only change when an audit re-syncs.
"""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from tools.build_dashboard import REPO_ROOT
from utils.logger import get_logger

log = get_logger("dashboard.content")

# Images are proxied through the dashboard rather than hotlinked: same-origin means
# they render regardless of CDN referer rules, and the on-disk cache keeps the
# dashboard working offline. Only these hosts may be fetched — never accept an
# arbitrary URL from the query string (that would be an open proxy / SSRF hole).
ALLOWED_IMAGE_HOSTS = {"i.mscwlns.co", "cdn.mscwlns.co"}
IMAGE_CACHE_DIR = REPO_ROOT / "outputs" / "image_cache"


def is_allowed_image(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc in ALLOWED_IMAGE_HOSTS


def cached_image(url: str):
    """Return (bytes, content_type) for an allowed image URL, or (None, None).
    Serves from disk when available, otherwise fetches once and caches."""
    if not is_allowed_image(url):
        return None, None
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    path = IMAGE_CACHE_DIR / key
    meta = IMAGE_CACHE_DIR / f"{key}.type"
    if path.exists():
        ctype = meta.read_text(encoding="utf-8").strip() if meta.exists() else "image/jpeg"
        return path.read_bytes(), ctype
    try:
        import requests
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return None, None
        ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        if not ctype.startswith("image/"):
            return None, None
        path.write_bytes(resp.content)
        meta.write_text(ctype, encoding="utf-8")
        return resp.content, ctype
    except Exception as e:
        log.warning(f"image fetch failed {url}: {e}")
        return None, None


@lru_cache(maxsize=256)
def _zeus(url: str):
    """(images, reviews) for a URL from the Zeus cache. Never raises."""
    images, reviews = [], []
    try:
        from scraper.zeus_connector import get_zeus_images, get_zeus_reviews
        images = get_zeus_images(url) or []
        reviews = get_zeus_reviews(url) or []
    except Exception as e:                      # cache missing / unparseable
        log.warning(f"Zeus content unavailable for {url}: {e}")
    return images, reviews


def images_for(url: str) -> list:
    """Zeus images as plain dicts, in page order, grouped-ready."""
    images, _ = _zeus(url)
    return [{
        "url": i.url,
        "position": i.position,
        "type": i.image_type or "other",
        "label": i.label or "",
        "widget_type": i.widget_type or "",
        "index": i.index,
    } for i in images]


def images_grouped(url: str) -> list:
    """[{type, images:[...]}, ...] in a sensible display order."""
    order = ["hero", "banner", "carousel", "comparison", "section", "testimonial", "other"]
    buckets: dict = {}
    for img in images_for(url):
        buckets.setdefault(img["type"] or "other", []).append(img)
    out = [{"type": t, "images": buckets.pop(t)} for t in order if t in buckets]
    out += [{"type": t, "images": v} for t, v in buckets.items()]
    return out


def reviews_for(url: str) -> list:
    """Zeus reviews as plain dicts (author, rating, date, title, text)."""
    _, reviews = _zeus(url)
    out = []
    for r in reviews:
        out.append({
            "author": getattr(r, "author", None) or "Anonymous",
            "rating": getattr(r, "rating", None),
            "date": getattr(r, "date", None) or "",
            "title": getattr(r, "title", None) or "",
            "text": getattr(r, "text", "") or "",
        })
    return out


_PKG_DIR = REPO_ROOT / "outputs" / "packaging_dashboard"


def packaging_photos(product_name: str) -> list:
    """Packaging vs PDP comparison photos for a product (from the extracted
    manifest). Returns [{sku_name, version, packaging:[url], pdp:[url]}]."""
    slug = re.sub(r"[^\w]", "_", product_name.lower()).strip("_")
    manifest = _PKG_DIR / f"{slug}.json"
    if not manifest.exists():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for sku in data.get("skus", []):
        out.append({
            "sku_name": sku.get("sku_name", ""),
            "version": sku.get("version", ""),
            "packaging": [f"/pkgimg/{slug}/{f}" for f in sku.get("packaging", [])],
            "pdp": [f"/pkgimg/{slug}/{f}" for f in sku.get("pdp", [])],
        })
    return out


def packaging_image_path(slug: str, filename: str):
    """Safe absolute path to an extracted packaging image, or None."""
    if "/" in filename or "\\" in filename or ".." in filename or "/" in slug or ".." in slug:
        return None
    path = (_PKG_DIR / slug / filename).resolve()
    if _PKG_DIR.resolve() not in path.parents or not path.is_file():
        return None
    return path


def content_stats(url: str) -> dict:
    images, reviews = _zeus(url)
    return {"images": len(images), "reviews": len(reviews)}
