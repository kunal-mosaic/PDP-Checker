"""
data.py — the ONLY layer the app reads through. It joins the product catalog
(config.yaml) with audit data from the SQLite store (dashboard/store.py).

Swapping the store's backend (SQLite → Postgres later) changes nothing here.
"""

from utils.config_loader import load_config
from dashboard import findings as findings_mod
from dashboard import store
from tools.build_dashboard import (
    DIMENSION_LABELS,
    _find_report,
    _product_slug,
    _source_health,
    _status_for,
)

__all__ = ["DIMENSION_LABELS", "list_categories", "get_category", "latest_report_path", "portfolio_stats"]


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
    "Narrative × Persona": "persona_narrative",
    "Visual": "visual_design",
    "Copy / Text": "copy_health",
    "Reviews": "reviews",
    "Ad Alignment": "ad_alignment",
}


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
