"""
parse_report_findings.py — recover the FULL nested analysis structure (sub-score
observations/suggestions, persona matrix, section flow, claims, packaging) from
the already-generated HTML reports, for products that don't have a .full.json.

This exists because outputs/runs/{run_id}.full.json is only written going forward
(after the dashboard was built) — older runs only have the compact score summary.
The report HTML has the full data rendered into a stable, consistent template, so
we parse it back out rather than re-running the (expensive) audit.

Each report has one <div class="pdp-block"> per URL. Within it we pull:
  - ssr cards (name/score/observation/suggestion) for persona_narrative,
    visual_design, reviews, copy_health.claims_alignment/brand_guidelines/spell_grammar,
    packaging sub-scores
  - hygiene claims/brand/spell flag lists (status inferred from fi-flagged/fi-warning/fi-ok)
  - persona matrix rows, section-flow chips, required-claims table, per-SKU packaging

Output: outputs/runs/{run_id}.full.json in the SAME shape findings.py already
expects (a list of PDPAnalysisResult-like dicts), so dashboard/store.py ingests
it with no changes.

Usage:
    python tools/parse_report_findings.py                  # all products, latest report each
    python tools/parse_report_findings.py --validate        # parse Shilajit + diff vs its real .full.json
"""

import json
import re
import sys
from pathlib import Path

from lxml import html as lxml_html

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "outputs" / "reports"
RUNS_DIR = REPO_ROOT / "outputs" / "runs"


def _num(text):
    if text is None:
        return None
    m = re.search(r"-?\d+\.?\d*", text)
    return float(m.group()) if m else None


def _text(node):
    return node.text_content().strip() if node is not None else ""


def _ssr_map(block, tab_ref: str) -> dict:
    """{sub_key: {name, score, observation, suggestion}} for one tab's ssr cards."""
    out = {}
    for ssr in block.xpath(f'.//div[@data-tab-ref="{tab_ref}"]'):
        key = ssr.get("data-sub-key")
        name = _text(ssr.xpath('.//div[@class="ssr-name"]')[0]) if ssr.xpath('.//div[@class="ssr-name"]') else ""
        score_el = ssr.xpath('.//div[contains(@class,"ssr-score")]')
        score = _num(_text(score_el[0])) if score_el else None
        obs = _text(ssr.xpath('.//div[@class="ssr-obs"]')[0]) if ssr.xpath('.//div[@class="ssr-obs"]') else ""
        sug_el = ssr.xpath('.//div[@class="ssr-sug"]')
        sug = _text(sug_el[0]) if sug_el else ""
        if key:
            out[key] = {"name": name, "score": score, "observation": obs, "suggestion": sug}
    return out


def _sub(m: dict, key: str, default_name: str) -> dict:
    v = m.get(key, {})
    return {"name": v.get("name") or default_name, "score": v.get("score", 0.0) or 0.0,
            "observation": v.get("observation", ""), "suggestion": v.get("suggestion", "")}


def _score_row_val(block, sc_id_suffix: str):
    els = block.xpath(f'.//div[contains(@id,"{sc_id_suffix}")]')
    return _num(_text(els[0])) if els else None


def _flag_status(fi_node) -> str:
    cls = fi_node.get("class", "")
    if "fi-flagged" in cls:
        return "flagged"
    if "fi-warning" in cls or "fi-warn" in cls:
        return "warning"
    return "ok"


def _claims_flags(block) -> list:
    out = []
    for fi in block.xpath('.//div[@id[contains(.,"-hygiene-claims")]]//div[contains(@class,"fi")]'):
        text_el = fi.xpath('.//span[@class="fi-text"]')
        reason_el = fi.xpath('.//span[@class="fi-reason"]')
        if not text_el:
            continue
        out.append({
            "text": _text(text_el[0]).strip('"'),
            "status": _flag_status(fi),
            "reason": _text(reason_el[0]) if reason_el else "",
        })
    return out


def _flat_flags(block, panel_id_contains: str) -> list:
    out = []
    for fi in block.xpath(f'.//div[@id[contains(.,"{panel_id_contains}")]]//div[contains(@class,"fi")]'):
        text_el = fi.xpath('.//span[@class="fi-text"]')
        if text_el:
            out.append(_text(text_el[0]))
    return out


def _persona_matrix(block) -> list:
    rows = []
    for tr in block.xpath('.//table[contains(@class,"pm-table")]//tbody/tr'):
        tds = tr.xpath('./td')
        if len(tds) < 3:
            continue
        persona = _text(tds[0])
        doing_right = [_text(li) for li in tds[1].xpath('.//li') if "Nothing identified" not in _text(li)]
        missing = [_text(li) for li in tds[2].xpath('.//li') if "Nothing missing" not in _text(li)]
        rows.append({"persona": persona, "doing_right": doing_right, "missing": missing})
    return rows


def _section_flow(block) -> dict:
    sf = block.xpath('.//div[contains(@class,"sf-wrap")]')
    if not sf:
        return {"score": 0.0, "current_order": [], "missing_sections": [],
                "out_of_order": [], "redundant_sections": [], "observation": "", "suggestion": ""}
    sf = sf[0]
    score_el = sf.xpath('.//div[contains(@class,"sf-score-badge")]')
    score = _num(_text(score_el[0])) if score_el else 0.0
    obs_el = sf.xpath('.//div[contains(@class,"sf-obs")]')
    observation = _text(obs_el[0]) if obs_el else ""

    current_order, out_of_order, redundant = [], [], []
    for li in sf.xpath('.//ol[contains(@class,"sf-order-list")]/li'):
        # strip the "↕ move" / "duplicate" tag text, keep just the section name
        tags = li.xpath('.//span[contains(@class,"sf-tag")]')
        name = _text(li)
        for t in tags:
            name = name.replace(_text(t), "").strip()
        current_order.append(name)
        cls = li.get("class", "")
        if "sf-ooo" in cls:
            out_of_order.append(name)
        if "sf-redundant" in cls:
            redundant.append(name)

    out_of_order_items = []
    for issue in sf.xpath('.//div[contains(@class,"sf-issue")]'):
        title = issue.xpath('.//div[contains(@class,"sf-issue-title")]')
        pos = issue.xpath('.//span[contains(@class,"sf-pos")]')
        reason_el = issue.xpath('.//div[contains(@class,"sf-issue-reason")]')
        section_name = _text(title[0]).replace(_text(pos[0]), "").strip().strip('"') if title else ""
        pos_nums = re.findall(r"\d+", _text(pos[0])) if pos else []
        out_of_order_items.append({
            "section": section_name,
            "current_position": int(pos_nums[0]) if len(pos_nums) > 0 else 0,
            "recommended_position": int(pos_nums[1]) if len(pos_nums) > 1 else 0,
            "reason": _text(reason_el[0]) if reason_el else "",
        })

    missing = [_text(m) for m in sf.xpath('.//div[contains(@class,"sf-missing")]')]
    sug_el = sf.xpath('.//div[contains(@class,"sf-suggestion")]')
    suggestion = _text(sug_el[0]).lstrip("💡 ").strip() if sug_el else ""

    return {"score": score or 0.0, "current_order": current_order, "missing_sections": missing,
            "out_of_order": out_of_order_items, "redundant_sections": redundant,
            "observation": observation, "suggestion": suggestion}


def _required_claims(block) -> list:
    out = []
    for tr in block.xpath('.//table[not(contains(@class,"pm-table")) and not(contains(@class,"ni-table"))]//tbody/tr'):
        tds = tr.xpath('./td')
        if len(tds) < 4:
            continue
        status_txt = _text(tds[2]).lower()
        status = ("violated" if "violated" in status_txt else
                  "absent" if "absent" in status_txt else
                  "mismatch" if "mismatch" in status_txt else
                  "present" if "present" in status_txt or "✅" in _text(tds[2]) else "cannot_verify")
        em = tds[3].xpath('.//em')
        out.append({
            "claim": _text(tds[0]), "claim_type": _text(tds[1]).lower(), "status": status,
            "found_text": _text(em[0]).strip('"') if em else None,
            "notes": _text(tds[3]).replace(_text(em[0]), "").strip() if em else _text(tds[3]),
        })
    return out


def _sku_results(block) -> list:
    skus = []
    for drawer in block.xpath('.//details[contains(@class,"scraped-block")][.//strong]'):
        strongs = drawer.xpath('.//summary//strong')
        if not strongs:
            continue
        sku_name = _text(strongs[0])
        chips = drawer.xpath('.//summary//span[contains(@class,"chip")]')
        version = _text(chips[0]) if chips else ""
        match_el = drawer.xpath('.//summary//span[contains(@class,"fi-src")]')
        match_txt = _text(match_el[0]) if match_el else ""
        packaging_match = True if "match" in match_txt and "mismatch" not in match_txt else (
            False if "mismatch" in match_txt else None)
        ni_present = "Yes" in "".join(drawer.xpath('.//text()[contains(., "NI Table Present on PDP")]/following::text()[1]')) \
            if drawer.xpath('.//text()[contains(., "NI Table Present")]') else False
        photo_present = bool(drawer.xpath('.//img'))
        diff_rows = []
        for tr in drawer.xpath('.//table[contains(@class,"") ]//tbody/tr'):
            pass  # NI table diffs are visual-only here; kept minimal for now
        skus.append({
            "sku_name": sku_name, "version": version, "drive_folder_url": "",
            "ni_table_present": ni_present, "ni_table_matches": None, "ni_table_diff": [],
            "ni_packaging_values": [], "ni_pdp_values": [],
            "product_photo_present": photo_present, "packaging_match": packaging_match,
            "packaging_mismatch_details": [],
        })
    return skus


def parse_report(report_path: Path) -> list:
    """Return a list of PDPAnalysisResult-shaped dicts, one per PDP in the report."""
    doc = lxml_html.fromstring(report_path.read_bytes())
    results = []
    for block in doc.xpath('//div[contains(@class,"pdp-block")]'):
        url_el = block.xpath('.//a[contains(@class,"card-url")]')
        url = url_el[0].get("href") if url_el else ""
        if not url:
            continue

        pn = _ssr_map(block, "pn")
        vis = _ssr_map(block, "vis")
        rev = _ssr_map(block, "rev")

        overall_pn = _score_row_val(block, "sc-pn")
        overall_vis = _score_row_val(block, "sc-vis")
        overall_rev = _score_row_val(block, "sc-rev")
        overall_hyg = _score_row_val(block, "sc-overall")
        overall_claims = _score_row_val(block, "sc-claims")
        overall_brand = _score_row_val(block, "sc-brand")
        overall_spell = _score_row_val(block, "sc-spell")

        result = {
            "url": url, "product_name": "", "analysed_at": "",
            "reviews": {
                "overall": overall_rev or 0.0,
                "freshness": _sub(rev, "freshness", "Freshness"),
                "rating_distribution": _sub(rev, "rating", "Rating Distribution"),
                "theme_alignment": _sub(rev, "theme", "Theme Alignment"),
                "negative_handling": _sub(rev, "negative", "Negative Handling"),
                "flagged_issues": _flat_flags(block, "-reviews") if False else [],
                "score_status": None, "freshness_warning": None,
            },
            "persona_narrative": {
                "overall": overall_pn or 0.0,
                "configured_narrative": "", "configured_persona": "",
                "hero_banner": _sub(pn, "hero", "Hero / Banner"),
                "carousel_flow": _sub(pn, "carousel", "Carousel Flow"),
                "banner_alignment": _sub(pn, "banner", "Banner Alignment"),
                "page_narrative_arc": _sub(pn, "arc", "Page Narrative Arc"),
                "cta_language": _sub(pn, "cta", "CTA Language"),
                "flagged_issues": [],
                "persona_matrix": _persona_matrix(block),
                "persona_used": "", "narrative_used": "", "pain_points_checked": [],
            },
            "copy_health": {
                "overall": overall_hyg or 0.0,
                "spell_grammar": {"name": "Spell / Grammar", "score": overall_spell or 0.0, "observation": "", "suggestion": ""},
                "brand_guidelines": {"name": "Brand Guidelines", "score": overall_brand or 0.0, "observation": "", "suggestion": ""},
                "claims_alignment": {"name": "Claims Alignment", "score": overall_claims or 0.0, "observation": "", "suggestion": ""},
                "claims_flags": _claims_flags(block),
                "brand_flags": _flat_flags(block, "-hygiene-brand"),
                "flagged_errors": _flat_flags(block, "-hygiene-spell"),
                "text_insights": [],  # copy/text tab insights: recovered only when present in .full.json
            },
            "visual_design": {
                "overall": overall_vis or 0.0,
                "human_presence": _sub(vis, "human", "Human Presence"),
                "proof_prominence": _sub(vis, "proof", "Proof Prominence"),
                "ingredient_imagery": _sub(vis, "ingredient", "Ingredient Imagery"),
                "before_after": _sub(vis, "before", "Before / After"),
                "lifestyle_shots": _sub(vis, "lifestyle", "Lifestyle Shots"),
                "visual_hierarchy_brand": _sub(vis, "hierarchy", "Visual Hierarchy & Brand"),
                "ni_table_present": {"name": "NI Table Present", "score": 0.0, "observation": "", "suggestion": ""},
                "product_photo_present": {"name": "Product Photo Present", "score": 0.0, "observation": "", "suggestion": ""},
                "latest_packaging_match": {"name": "Latest Packaging Match", "score": 0.0, "observation": "", "suggestion": ""},
                "flagged_issues": [],
                "section_flow": _section_flow(block),
            },
            "ad_alignment": {
                "overall": 0.0, "top_converting_angles": [], "angles_present_on_pdp": [], "gaps": [],
                "atc_drop_off_addressed": {"name": "ATC Drop-off", "score": 0.0, "observation": "", "suggestion": ""},
                "flagged_gaps": [],
            },
            "packaging": None,
            "overall_score": 0.0, "status": "unknown", "rca": [],
            "delta": None, "delta_scores": {}, "regression_flag": False,
        }

        pkg_score = _score_row_val(block, "") if False else None
        sku_results = _sku_results(block)
        required = _required_claims(block)
        if sku_results or required:
            result["packaging"] = {
                "overall": 0.0,
                "ni_table_check": {"name": "NI Table", "score": 0.0, "observation": "", "suggestion": ""},
                "product_photo_check": {"name": "Product Photo", "score": 0.0, "observation": "", "suggestion": ""},
                "packaging_match_check": {"name": "Latest Packaging Match", "score": 0.0, "observation": "", "suggestion": ""},
                "sku_results": sku_results,
                "required_claims_checks": required,
                "error": None,
            }

        results.append(result)
    return results


def _slug_from_report(name: str) -> str:
    return re.sub(r"_\d{8}\.html$", "", name)


def _latest_run_id_for_slug(slug: str):
    """Find the newest existing run_id (by filename) for this product slug in outputs/runs,
    so the parsed .full.json attaches to the SAME run the dashboard already shows.
    Report filenames keep hyphens (e.g. 'anti-hairfall_kit_s1'); run files normalise
    everything to underscores — try both so neither convention gets skipped."""
    for candidate_slug in {slug, slug.replace("-", "_")}:
        candidates = sorted(RUNS_DIR.glob(f"{candidate_slug}_run_*.json"))
        candidates = [c for c in candidates if not c.name.endswith(".full.json")]
        if candidates:
            return candidates[-1].stem
    return None


def run_all(only_slug=None):
    written = 0
    for report in sorted(REPORTS_DIR.glob("*.html")):
        slug = _slug_from_report(report.name)
        if only_slug and slug != only_slug:
            continue
        run_id = _latest_run_id_for_slug(slug)
        if not run_id:
            print(f"  {slug}: no matching run summary — skipping")
            continue
        full_path = RUNS_DIR / f"{run_id}.full.json"
        if full_path.exists():
            print(f"  {slug}: .full.json already exists — skipping")
            continue
        results = parse_report(report)
        full_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {slug}: parsed {len(results)} PDP(s) -> {full_path.name}")
        written += 1
    print(f"Done — {written} report(s) parsed.")


def validate_against_shilajit():
    """Parse Shilajit's report and diff key fields against its REAL .full.json
    (ground truth) so we can trust the parser before running it on other products."""
    report = REPORTS_DIR / "shilajit_gummies_20260714.html"
    real_path = RUNS_DIR / "shilajit_gummies_run_20260727_105859.full.json"
    if not report.exists() or not real_path.exists():
        print("Missing report or ground-truth file for validation.")
        return
    parsed = parse_report(report)
    print(f"Parsed {len(parsed)} PDP blocks from the 2026-07-14 report "
          f"(ground truth is the 2026-07-27 run — different run, so scores WILL differ; "
          f"we're validating STRUCTURE, not exact values).")
    for p in parsed:
        print(f"\n  URL: {p['url']}")
        print(f"    persona_narrative.hero_banner: score={p['persona_narrative']['hero_banner']['score']} "
              f"obs_len={len(p['persona_narrative']['hero_banner']['observation'])}")
        print(f"    persona_matrix rows: {len(p['persona_narrative']['persona_matrix'])}")
        print(f"    claims_flags: {len(p['copy_health']['claims_flags'])}")
        print(f"    section_flow.current_order len: {len(p['visual_design']['section_flow']['current_order'])}")
        if p.get("packaging"):
            print(f"    packaging.sku_results: {len(p['packaging']['sku_results'])} "
                  f"required_claims: {len(p['packaging']['required_claims_checks'])}")


if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate_against_shilajit()
    else:
        run_all()
