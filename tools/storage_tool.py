"""
Storage Tool — persists run snapshots and loads previous runs.

Saves a compact JSON snapshot after every pipeline run so regression
detection and history trending have data to work from.

Snapshot format (outputs/runs/{product_slug}_run_YYYYMMDD_HHMMSS.json):
{
  "run_id":      "shilajit_gummies_run_20260525_143600",
  "run_date":    "2026-05-25",
  "run_ts":      "2026-05-25T14:36:00",
  "product_name":"Shilajit Gummies",
  "pdps": [
    {
      "url":           "https://...",
      "overall_score": 5.1,
      "status":        "critical",
      "scores": {
        "reviews":           4.2,
        "persona_narrative": 4.2,
        "copy_health":       6.2,
        "visual_design":     5.8,
        "ad_alignment":      5.0
      }
    }
  ]
}
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from analyser.models import PDPAnalysisResult
from utils.logger import get_logger

log = get_logger("storage_tool")

RUNS_DIR = Path("outputs/runs")


def _product_slug(product_name: str) -> str:
    return re.sub(r"[^\w]", "_", product_name.lower()).strip("_")


def save_run(product_name: str, results: List[PDPAnalysisResult]) -> Path:
    """
    Persist a compact snapshot of this run to outputs/runs/.
    Returns the path of the saved file.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.utcnow()
    slug = _product_slug(product_name)
    run_id = f"{slug}_run_{now.strftime('%Y%m%d_%H%M%S')}"
    filename = RUNS_DIR / f"{run_id}.json"

    snapshot = {
        "run_id":       run_id,
        "run_date":     now.strftime("%Y-%m-%d"),
        "run_ts":       now.isoformat(),
        "product_name": product_name,
        "pdps": [
            {
                "url":           r.url,
                "overall_score": r.overall_score,
                "status":        r.status,
                "scores": {
                    "reviews":           r.reviews.overall,
                    "persona_narrative": r.persona_narrative.overall,
                    "copy_health":       r.copy_health.overall,
                    "visual_design":     r.visual_design.overall,
                    "ad_alignment":      r.ad_alignment.overall,
                }
            }
            for r in results
        ]
    }

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    # Full findings snapshot — the complete PDPAnalysisResult per URL, so the
    # dashboard can render suggestions/findings (not just scores). Heavy base64
    # image blobs are stripped to keep the file small.
    full_path = RUNS_DIR / f"{run_id}.full.json"
    full = [_strip_blobs(r.model_dump()) for r in results]
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Full findings saved → {full_path}")

    log.info(f"Run snapshot saved → {filename}")
    return filename


_BLOB_FIELDS = ("packaging_images_b64", "pdp_product_images_b64", "ni_table_pdp_image_b64")


def _strip_blobs(result: dict) -> dict:
    """Remove large base64 image fields from a serialized result (in place)."""
    pkg = result.get("packaging")
    if pkg:
        for sku in pkg.get("sku_results", []):
            for field in _BLOB_FIELDS:
                sku.pop(field, None)
    return result


def load_previous_run(product_name: str, before_run_id: str = None) -> Optional[dict]:
    """
    Load the most recent previous run snapshot for a product.

    If before_run_id is provided, loads the run immediately before it
    (so the current run doesn't compare against itself).
    Returns the snapshot dict, or None if no previous run exists.
    """
    slug = _product_slug(product_name)
    pattern = f"{slug}_run_*.json"
    run_files = sorted(RUNS_DIR.glob(pattern))  # sorted ascending by filename = by date

    if not run_files:
        log.info(f"No previous runs found for '{product_name}'")
        return None

    if before_run_id:
        # Exclude the file that matches the current run_id
        run_files = [f for f in run_files if before_run_id not in f.name]

    if not run_files:
        log.info(f"No previous run found before current run")
        return None

    latest = run_files[-1]
    log.info(f"Loading previous run → {latest.name}")

    with open(latest) as f:
        return json.load(f)


def list_runs(product_name: str) -> List[dict]:
    """
    Return all run snapshots for a product, oldest first.
    Useful for history trend charts.
    """
    slug = _product_slug(product_name)
    pattern = f"{slug}_run_*.json"
    run_files = sorted(RUNS_DIR.glob(pattern))

    runs = []
    for f in run_files:
        try:
            with open(f) as fp:
                runs.append(json.load(fp))
        except Exception as e:
            log.warning(f"Could not read {f.name}: {e}")

    return runs
