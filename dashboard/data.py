"""
data.py — read-only views the dashboard renders from: the product catalog
(config.yaml) joined with each product's latest audit run summary.

Reuses the pure helpers in tools/build_dashboard.py so there is one source of
truth for "latest run", status bands, source-health and report lookup.
"""

from utils.config_loader import load_config
from tools.build_dashboard import (
    DIMENSION_LABELS,
    _find_report,
    _latest_run_per_product,
    _product_overall,
    _product_slug,
    _source_health,
    _status_for,
)

__all__ = ["DIMENSION_LABELS", "list_categories", "get_category", "latest_report_path", "portfolio_stats"]


def list_categories() -> list:
    """One row per product: catalog info + latest-run scores/status."""
    cfg = load_config()
    runs = _latest_run_per_product()
    cats = []
    for p in cfg["products"]:
        name = p["name"]
        run = runs.get(name)
        overall = _product_overall(run) if run else None
        cats.append({
            "name": name,
            "slug": _product_slug(name),
            "url_count": len(p.get("urls", [])),
            "score": overall,
            "status": _status_for(overall),
            "run_ts": (run or {}).get("run_ts"),
            "pdps": (run or {}).get("pdps", []),
            "source_stub": _source_health(name).get("stub", False),
            "has_report": _find_report(name) is not None,
        })
    return cats


def get_category(slug: str):
    for c in list_categories():
        if c["slug"] == slug:
            return c
    return None


def latest_report_path(slug: str):
    """Absolute path to a product's latest full HTML report, or None."""
    for p in load_config()["products"]:
        if _product_slug(p["name"]) == slug:
            return _find_report(p["name"])
    return None


def portfolio_stats(cats: list) -> dict:
    counts = {"healthy": 0, "attention": 0, "critical": 0, "unknown": 0}
    scores = []
    for c in cats:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
        if c["score"] is not None:
            scores.append(c["score"])
    counts["total"] = len(cats)
    counts["avg"] = round(sum(scores) / len(scores), 1) if scores else "—"
    return counts
