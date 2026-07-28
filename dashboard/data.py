"""
data.py — the ONLY layer the app reads through. It joins the product catalog
(config.yaml) with audit data from the SQLite store (dashboard/store.py).

Swapping the store's backend (SQLite → Postgres later) changes nothing here.
"""

import json

from utils.config_loader import load_config
from dashboard import content
from dashboard import findings as findings_mod
from dashboard import store
from tools.build_dashboard import (
    DIMENSION_LABELS,
    RUNS_DIR,
    _find_report,
    _product_slug,
    _source_health,
    _status_for,
)


def _raw_result(run_id: str, url: str) -> dict:
    """The full PDPAnalysisResult dict for one URL, from the run's .full.json —
    lets the PDP page render the report's full structure (sub-score observations,
    persona matrix, section flow, packaging) natively, not just flattened findings."""
    if not run_id:
        return {}
    path = RUNS_DIR / f"{run_id}.full.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return next((r for r in data if r.get("url") == url), {})

__all__ = ["DIMENSION_LABELS", "PDP_TABS", "list_categories", "get_category", "get_pdp",
           "latest_report_path", "portfolio_stats"]

# Tabs on the PDP workspace. "kind" drives what the tab renders:
#   content  → page assets (images / reviews) plus that section's findings
#   findings → findings only
#   history  → run history
# Tabs mirror the HTML report's sections, in order, then extras (Ad Gaps, RCA, History).
PDP_TABS = [
    {"id": "summary",   "label": "Summary",             "kind": "overview", "section": None},
    {"id": "hygiene",   "label": "Hygiene Check",       "kind": "findings", "section": "Hygiene Check"},
    {"id": "narrative", "label": "Narrative × Persona", "kind": "findings", "section": "Narrative × Persona"},
    {"id": "visual",    "label": "Visual Layer",        "kind": "images",   "section": "Visual Layer"},
    {"id": "text",      "label": "Text Layer",          "kind": "findings", "section": "Text Layer"},
    {"id": "reviews",   "label": "Reviews & Ratings",   "kind": "reviews",  "section": "Reviews & Ratings"},
    {"id": "packaging", "label": "Product Packaging",   "kind": "findings", "section": "Product Packaging"},
    {"id": "ads",       "label": "Ad Gaps",             "kind": "findings", "section": "Ad Gaps"},
    {"id": "rca",       "label": "Root Cause",          "kind": "findings", "section": "Root Cause"},
    {"id": "history",   "label": "History",             "kind": "history",  "section": None},
]


def _overall(pdps: list):
    scores = [p["overall_score"] for p in pdps if p.get("overall_score") is not None]
    return round(sum(scores) / len(scores), 1) if scores else None


def list_categories() -> list:
    """One row per product: catalog info + latest-run scores/status from the store."""
    store.sync()  # cheap no-op once everything's ingested; picks up any new runs
    cfg = load_config()
    cats = []
    for p in cfg["products"]:
        name = p["name"]
        run = store.latest_run(name)
        run_id = run.get("run_id")
        pdps = store.pdps_for_run(run_id) if run_id else []
        overall = _overall(pdps)
        cats.append({
            "name": name,
            "slug": _product_slug(name),
            "url_count": len(p.get("urls", [])),
            "score": overall,
            "status": _status_for(overall),
            "run_ts": run.get("run_ts"),
            "run_id": run_id,
            "pdps": pdps,
            "source_stub": _source_health(name).get("stub", False),
            "has_report": _find_report(name) is not None,
        })
    return cats


def get_category(slug: str):
    """Category detail with findings attached to each PDP (from the store)."""
    cat = next((c for c in list_categories() if c["slug"] == slug), None)
    if not cat:
        return None
    grouped = store.findings_for_run(cat["run_id"], product=cat["name"]) if cat.get("run_id") else {}
    total = {"critical": 0, "warning": 0, "info": 0, "total": 0, "addressed": 0}
    for pdp in cat["pdps"]:
        fnd = grouped.get(pdp["url"], [])
        pdp["findings"] = fnd
        pdp["finding_summary"] = _summarize_open(fnd)
        # Group into report-style sections; counts reflect OPEN items, dimension score tagged.
        sections = findings_mod.group_by_section(fnd)
        for sec in sections:
            sec["summary"] = _summarize_open(sec["findings"])
            key = _SECTION_SCORE_KEY.get(sec["section"])
            sec["score"] = pdp["scores"].get(key) if key else None
        pdp["sections"] = sections
        for k in total:
            total[k] += pdp["finding_summary"].get(k, 0)
    cat["finding_summary"] = total
    cat["has_findings"] = total["total"] > 0
    return cat


def _summarize_open(findings: list) -> dict:
    """Severity counts for OPEN findings only, plus how many are addressed."""
    open_fnd = [f for f in findings if f.get("status", "open") == "open"]
    summary = findings_mod.summarize(open_fnd)          # critical/warning/info of open items
    summary["addressed"] = len(findings) - len(open_fnd)
    summary["total"] = len(findings)
    return summary


# Maps a section to the dimension score in the run summary (where one exists).
_SECTION_SCORE_KEY = {
    "Hygiene Check": "copy_health",
    "Narrative × Persona": "persona_narrative",
    "Visual Layer": "visual_design",
    "Text Layer": "copy_health",
    "Reviews & Ratings": "reviews",
    "Ad Gaps": "ad_alignment",
}


def get_pdp(slug: str, idx: int):
    """One PDP as a full workspace: scores, sections of findings, page content
    (Zeus images + reviews) and its run history."""
    cat = get_category(slug)
    if not cat or idx < 0 or idx >= len(cat["pdps"]):
        return None, None
    pdp = cat["pdps"][idx]
    url = pdp["url"]
    pdp["index"] = idx
    pdp["images_grouped"] = content.images_grouped(url)
    pdp["image_count"] = sum(len(g["images"]) for g in pdp["images_grouped"])
    pdp["reviews"] = content.reviews_for(url)
    pdp["history"] = store.history_for_url(cat["name"], url)
    # Findings keyed by section name for direct tab lookup
    pdp["by_section"] = {s["section"]: s for s in pdp["sections"]}
    # Full raw result → render the report's native structure (sub-scores, matrix, …)
    pdp["raw"] = _raw_result(cat.get("run_id"), url)
    # Packaging comparison photos (extracted from the reports) — product-level
    pdp["packaging_photos"] = content.packaging_photos(cat["name"])
    return cat, pdp


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
