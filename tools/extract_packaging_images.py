"""
extract_packaging_images.py — pull packaging comparison photos out of the
already-generated HTML reports (base64) and save them for the dashboard.

The audit downloads packaging artwork + PDP product shots and embeds them in the
report as base64. The dashboard's .full.json strips those blobs (to stay small),
so this one-time script recovers them from the reports instead of re-running —
zero API cost.

For each report it finds the Per-SKU packaging drawers and their two labelled
image columns (Latest Packaging vs PDP Product Images), decodes the base64, and
writes:
    outputs/packaging_dashboard/{slug}/{sku}_{col}_{i}.jpg
    outputs/packaging_dashboard/{slug}.json   (manifest the dashboard reads)

Usage:
    python tools/extract_packaging_images.py
"""

import base64
import json
import re
from pathlib import Path

from lxml import html as lxml_html

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "outputs" / "reports"
OUT_DIR = REPO_ROOT / "outputs" / "packaging_dashboard"

_DATA_URI = re.compile(r"^data:image/[a-zA-Z]+;base64,(.+)$", re.DOTALL)


def _slug_from_report(name: str) -> str:
    return re.sub(r"_\d{8}\.html$", "", name)


def _decode(src: str):
    m = _DATA_URI.match(src.strip())
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1))
    except Exception:
        return None


def _imgs_in(node) -> list:
    out = []
    for img in node.xpath(".//img"):
        raw = _decode(img.get("src", ""))
        if raw:
            out.append(raw)
    return out


def _extract_report(report_path: Path) -> dict:
    """Return {skus: [{sku_name, version, packaging:[bytes], pdp:[bytes]}]} for one report."""
    doc = lxml_html.fromstring(report_path.read_bytes())
    skus = []
    seen = set()
    # Packaging panels (one per URL result); take drawers from all, dedupe by SKU name.
    for panel in doc.xpath("//div[contains(@id,'-packaging')]"):
        for drawer in panel.xpath(".//details[contains(@class,'scraped-block')]"):
            strongs = drawer.xpath(".//summary//strong")
            sku_name = strongs[0].text_content().strip() if strongs else "SKU"
            if sku_name in seen:
                continue
            grids = drawer.xpath(".//div[contains(@style,'grid-template-columns:1fr 1fr')]")
            if not grids:
                continue
            cols = grids[0].xpath("./div")
            if len(cols) < 2:
                continue
            packaging = _imgs_in(cols[0])
            pdp = _imgs_in(cols[1])
            if not packaging and not pdp:
                continue
            seen.add(sku_name)
            chips = drawer.xpath(".//summary//span[contains(@class,'chip')]")
            version = chips[0].text_content().strip() if chips else ""
            skus.append({"sku_name": sku_name, "version": version, "packaging": packaging, "pdp": pdp})
    return {"skus": skus}


def _latest_report_per_slug() -> dict:
    latest = {}
    for f in sorted(REPORTS_DIR.glob("*.html")):
        latest[_slug_from_report(f.name)] = f      # sorted → last (newest date) wins
    return latest


def run() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_imgs = 0
    for slug, report in _latest_report_per_slug().items():
        data = _extract_report(report)
        if not data["skus"]:
            continue
        slug_dir = OUT_DIR / slug
        slug_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"slug": slug, "skus": []}
        for si, sku in enumerate(data["skus"]):
            entry = {"sku_name": sku["sku_name"], "version": sku["version"], "packaging": [], "pdp": []}
            for col in ("packaging", "pdp"):
                for i, blob in enumerate(sku[col]):
                    fname = f"{si}_{col}_{i}.jpg"
                    (slug_dir / fname).write_bytes(blob)
                    entry[col].append(fname)
                    total_imgs += 1
            manifest["skus"].append(entry)
        (OUT_DIR / f"{slug}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"  {slug}: {len(manifest['skus'])} SKU(s), "
              f"{sum(len(s['packaging']) + len(s['pdp']) for s in manifest['skus'])} images")
    print(f"Done — {total_imgs} packaging images extracted to {OUT_DIR}")
    return total_imgs


if __name__ == "__main__":
    run()
