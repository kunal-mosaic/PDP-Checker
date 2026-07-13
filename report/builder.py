from pathlib import Path
from datetime import datetime
from typing import List, Optional
from jinja2 import Environment, FileSystemLoader
from analyser.models import PDPAnalysisResult
from scraper.models import PDPTextData
from scraper.sheets_models import SheetsData
from utils.config_loader import load_config
_config = load_config()
from utils.logger import get_logger

log = get_logger("report_builder")
config = load_config()
REPORTS_DIR = Path(config["report"]["output_dir"])


def _status_emoji(status: str) -> str:
    return {"healthy": "✅", "attention": "⚠️", "critical": "🔴"}.get(status, "")


def _score_class(score: float) -> str:
    if score >= 8.0: return "healthy"
    if score >= 6.5: return "attention"
    return "critical"


def _score_bar(score: float) -> int:
    """Convert score to percentage for progress bar."""
    return int((score / 10) * 100)


def _top_ad_angles(sheets: SheetsData, top_n: int = 5) -> List[dict]:
    """Return top N ads sorted by Conv.% with parsed angle."""
    ads = [a for a in sheets.top_ads if a.conversion_rate is not None]
    ads = sorted(ads, key=lambda a: a.conversion_rate, reverse=True)[:top_n]
    result = []
    for ad in ads:
        # Clean up the hook name into readable angle
        parts = ad.hook.lower().split("_")
        stop = {"int","vo","inf","bof","tof","mof","hindi","english","hinglish",
                "static","reel","ugc","none","pan","india","si","bca","asc","cbo"}
        angle = " ".join(p for p in parts[2:-1] if p not in stop and len(p) > 2)
        result.append({
            "angle": angle.replace("_", " ").title(),
            "conv": f"{ad.conversion_rate:.1f}%",
            "atc":  f"{ad.atc_rate:.1f}%" if ad.atc_rate else "—",
            "rank": ad.ranking or "—"
        })
    return result


def _build_scrape_map(pdp_list: Optional[List[PDPTextData]]) -> dict:
    """Map URL → PDPTextData for template lookups."""
    if not pdp_list:
        return {}
    return {pdp.url: pdp for pdp in pdp_list}


def _detect_narrative(pdp: PDPTextData) -> dict:
    """
    Identify persona and narrative from PDP copy.
    Based on headline + first subheads, matched against the
    Shilajit Gummies narrative brief (Tejas / Aakash / Fitness Buyer).
    """
    headline = (pdp.headline or "").lower()
    subs     = " ".join(pdp.subheads[:6]).lower() if pdp.subheads else ""
    body     = " ".join(pdp.body_copy[:5]).lower() if pdp.body_copy else ""
    copy     = headline + " " + subs + " " + body

    # Narrative detection heuristics — ordered by specificity
    if any(w in copy for w in ["clinical", "study", "cortisol", "25%", "90-day", "placebo", "peer-reviewed"]):
        return {"persona": "Fitness Buyer", "narrative": "Clinical Studies", "signal": "Clinical proof language in copy"}
    if any(w in copy for w in ["resin", "vs gummy", "format", "travel-ready", "bitter"]):
        return {"persona": "Tejas / Fitness Buyer", "narrative": "Product First — Resin vs Gummy", "signal": "Resin comparison copy"}
    if any(w in copy for w in ["sugar", "no added sugar", "chicory", "glucose spike", "sugar-free"]):
        return {"persona": "Tejas / Aakash", "narrative": "Zero Added Sugar / Pure & Natural", "signal": "Sugar-free messaging"}
    if any(w in copy for w in ["energy", "3 pm", "afternoon", "crash", "tired", "meeting", "work", "daily energy"]):
        return {"persona": "Tejas", "narrative": "Daily Energy", "signal": "Energy / afternoon crash copy"}
    if any(w in copy for w in ["natural", "clean", "ingredient", "no chemical", "no preservative"]):
        return {"persona": "Aakash", "narrative": "Pure & Natural / Ingredients", "signal": "Clean ingredient messaging"}

    # Hero image label fallback
    return {"persona": "Tejas", "narrative": "Daily Energy", "signal": "Default (no strong signal)"}


def build_report(
    results: List[PDPAnalysisResult],
    sheets: SheetsData,
    product_name: str,
    pdp_list: Optional[List[PDPTextData]] = None,
    regression_alerts: Optional[list] = None,
    packaging_only: bool = False,
) -> str:
    """
    Build HTML report from analysis results.
    Returns path to the saved report file.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(Path(__file__).parent))
    env.filters["score_class"] = _score_class
    env.filters["score_bar"]   = _score_bar
    template = env.get_template("template.html")

    # Overall product health = average of all PDP scores
    avg_score  = round(sum(r.overall_score for r in results) / len(results), 1) if results else 0
    date_str   = datetime.now().strftime("%d %B %Y")
    top_ads    = _top_ad_angles(sheets)
    scrape_map = _build_scrape_map(pdp_list)

    # Build narrative map: URL → {persona, narrative, signal}
    # Prefer config-defined narrative; fall back to auto-detect
    cfg_url_map = {}
    for prod in _config.get("products", []):
        for u in prod.get("urls", []):
            if isinstance(u, dict):
                cfg_url_map[u["url"]] = u

    narrative_map = {}
    if pdp_list:
        for pdp in pdp_list:
            cfg = cfg_url_map.get(pdp.url, {})
            if cfg.get("narrative"):
                narrative_map[pdp.url] = {
                    "persona":   cfg.get("persona", ""),
                    "narrative": cfg.get("narrative", ""),
                    "signal":    "Configured in config.yaml"
                }
            else:
                narrative_map[pdp.url] = _detect_narrative(pdp)

    regression_count = len(regression_alerts) if regression_alerts else 0

    html = template.render(
        product_name=product_name,
        date=date_str,
        avg_score=avg_score,
        avg_status=_score_class(avg_score),
        status_emoji=_status_emoji(_score_class(avg_score)),
        results=results,
        top_ads=top_ads,
        scrape_map=scrape_map,
        narrative_map=narrative_map,
        score_class=_score_class,
        score_bar=_score_bar,
        regression_alerts=regression_alerts or [],
        regression_count=regression_count,
        packaging_only=packaging_only,
    )

    filename = f"{product_name.lower().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.html"
    output_path = REPORTS_DIR / filename
    run_ts = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    html = html + f"\n<!-- generated: {run_ts} -->"
    output_path.write_text(html, encoding="utf-8")

    log.info(f"Report saved → {output_path}")
    return str(output_path)
