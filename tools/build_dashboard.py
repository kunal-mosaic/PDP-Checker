"""
build_dashboard.py — generate a single self-contained portfolio dashboard from
existing audit run data.

Reads every per-product run summary in ``outputs/runs/*.json``, keeps the latest
run per product, and renders one static ``outputs/dashboard/index.html``:

    Portfolio overview  →  category cards (score · status · per-dimension bars)
                        →  per-PDP drill-down  →  link to the full HTML report

Zero external services, zero API cost — it only reads what previous audits
already produced. Re-run it any time after ``python main.py --run-now`` to refresh.

Usage:
    python tools/build_dashboard.py
"""

import html
import json
import re
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "outputs" / "runs"
REPORTS_DIR = REPO_ROOT / "outputs" / "reports"
MASTERDOCS_DIR = REPO_ROOT / "inputs" / "masterdocs"
OUT_DIR = REPO_ROOT / "outputs" / "dashboard"

# Human labels for the score dimensions stored in each run JSON.
DIMENSION_LABELS = {
    "persona_narrative": "Narrative × Persona",
    "visual_design": "Visual Layer",
    "copy_health": "Hygiene / Copy",
    "reviews": "Reviews",
    "ad_alignment": "Ad Alignment",
    "packaging": "Packaging",
}


# ── data loading ────────────────────────────────────────────────────────────

def _product_slug(name: str) -> str:
    return re.sub(r"[^\w]", "_", name.lower()).strip("_")


def _status_for(score):
    if score is None:
        return "unknown"
    if score >= 8.0:
        return "healthy"
    if score >= 6.5:
        return "attention"
    return "critical"


def _latest_run_per_product() -> dict:
    """Return {product_name: run_dict} keeping only the most recent run each."""
    latest = {}
    for f in sorted(RUNS_DIR.glob("*.json")):
        if f.name.endswith(".full.json"):
            continue  # full-findings snapshots are lists, not run summaries
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        name = data.get("product_name")
        if not name:
            continue
        ts = data.get("run_ts", "")
        if name not in latest or ts > latest[name].get("run_ts", ""):
            latest[name] = data
    return latest


def _find_report(product_name: str):
    """Latest full HTML report for a product, or None. Handles both slug styles."""
    if not REPORTS_DIR.exists():
        return None
    builder_slug = product_name.lower().replace(" ", "_")
    candidates = []
    for slug in {builder_slug, _product_slug(product_name)}:
        candidates += list(REPORTS_DIR.glob(f"{slug}_*.html"))
    return sorted(candidates)[-1] if candidates else None


def _source_health(product_name: str) -> dict:
    """Rough read of masterdoc completeness so 'source health' shows on the card."""
    slug = _product_slug(product_name)
    for md in MASTERDOCS_DIR.glob("*.md"):
        if md.stem == slug or slug.startswith(md.stem) or md.stem.startswith(slug):
            text = md.read_text(encoding="utf-8", errors="ignore")
            lines = text.count("\n") + 1
            is_stub = lines < 45 or "[TBD" in text
            return {"found": True, "lines": lines, "stub": is_stub}
    return {"found": False, "lines": 0, "stub": True}


def _product_overall(run: dict):
    scores = [p.get("overall_score") for p in run.get("pdps", []) if p.get("overall_score") is not None]
    return round(sum(scores) / len(scores), 1) if scores else None


# ── rendering ────────────────────────────────────────────────────────────────

def _bar(label, score):
    if score is None:
        pct, cls, val = 0, "unknown", "—"
    else:
        pct, cls, val = int(score / 10 * 100), _status_for(score), f"{score:.1f}"
    return f"""
      <div class="dim">
        <span class="dim-label">{html.escape(label)}</span>
        <span class="dim-bar {cls}"><span style="width:{pct}%"></span></span>
        <span class="dim-val">{val}</span>
      </div>"""


def _pdp_row(pdp):
    url = html.escape(pdp.get("url", ""))
    overall = pdp.get("overall_score")
    status = pdp.get("status") or _status_for(overall)
    bars = "".join(
        _bar(DIMENSION_LABELS.get(k, k.replace("_", " ").title()), v)
        for k, v in (pdp.get("scores") or {}).items()
    )
    overall_txt = f"{overall:.1f}" if overall is not None else "—"
    return f"""
      <div class="pdp">
        <div class="pdp-head">
          <a class="pdp-url" href="{url}" target="_blank" rel="noopener">{url}</a>
          <span class="pill {status}">{status} · {overall_txt}</span>
        </div>
        <div class="dims">{bars}</div>
      </div>"""


def _card(name, run):
    overall = _product_overall(run)
    status = _status_for(overall)
    pdps = run.get("pdps", [])
    src = _source_health(name)
    report = _find_report(name)

    if report:
        rel = f"../reports/{report.name}"
        report_link = f'<a class="report-link" href="{html.escape(rel)}">View full report →</a>'
    else:
        report_link = (
            f'<span class="report-missing">No report yet · run: '
            f'<code>python main.py --run-now --product "{html.escape(name)}"</code></span>'
        )

    src_chip = (
        '<span class="chip stub">source: stub</span>' if src["stub"]
        else '<span class="chip ok">source: full</span>'
    )
    overall_txt = f"{overall:.1f}" if overall is not None else "—"
    pdp_rows = "".join(_pdp_row(p) for p in pdps)

    return f"""
    <details class="card {status}">
      <summary>
        <div class="card-top">
          <div class="card-title">
            <span class="cname">{html.escape(name)}</span>
            {src_chip}
          </div>
          <div class="score-block">
            <span class="score">{overall_txt}</span>
            <span class="pill {status}">{status}</span>
          </div>
        </div>
        <div class="card-meta">{len(pdps)} PDP(s) · {report_link}</div>
      </summary>
      <div class="card-body">{pdp_rows}</div>
    </details>"""


def build() -> Path:
    products = [p["name"] for p in yaml.safe_load((REPO_ROOT / "config.yaml").read_text())["products"]]
    runs = _latest_run_per_product()

    counts = {"healthy": 0, "attention": 0, "critical": 0, "unknown": 0}
    overalls, cards = [], []
    last_run = ""
    for name in products:
        run = runs.get(name)
        if not run:
            counts["unknown"] += 1
            cards.append(
                f'<details class="card unknown"><summary><div class="card-top">'
                f'<div class="card-title"><span class="cname">{html.escape(name)}</span></div>'
                f'<div class="score-block"><span class="score">—</span>'
                f'<span class="pill unknown">no audit</span></div></div>'
                f'<div class="card-meta">No run data yet</div></summary></details>'
            )
            continue
        overall = _product_overall(run)
        counts[_status_for(overall)] += 1
        if overall is not None:
            overalls.append(overall)
        last_run = max(last_run, run.get("run_ts", ""))
        cards.append(_card(name, run))

    avg_health = round(sum(overalls) / len(overalls), 1) if overalls else "—"
    last_run_txt = last_run[:10] if last_run else "never"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    template = (Path(__file__).parent / "_dashboard_template.html").read_text(encoding="utf-8")
    tokens = {
        "{cards}": "\n".join(cards),
        "{total}": str(len(products)),
        "{healthy}": str(counts["healthy"]),
        "{attention}": str(counts["attention"]),
        "{critical}": str(counts["critical"]),
        "{unknown}": str(counts["unknown"]),
        "{avg}": str(avg_health),
        "{last_run}": last_run_txt,
        "{generated}": generated,
    }
    page = template
    for token, value in tokens.items():
        page = page.replace(token, value)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "index.html"
    out.write_text(page, encoding="utf-8")
    print(f"Dashboard written → {out}")
    print(f"  {len(products)} products · {counts['healthy']} healthy · "
          f"{counts['attention']} attention · {counts['critical']} critical · "
          f"{counts['unknown']} no-data · avg {avg_health}")
    return out


if __name__ == "__main__":
    build()
