"""
findings.py — flatten a full PDPAnalysisResult (as a dict) into a prioritized,
actionable list of findings the dashboard renders.

Each finding has a STABLE id (a fingerprint of url + layer + key), so the same
issue keeps the same id across runs — the foundation P3 needs for "mark fixed →
re-verify" without re-flagging resolved items.

Finding = {
    "id":         "<12-char fingerprint>",
    "layer":      "Claims" | "Narrative × Persona" | "Visual" | ... ,
    "severity":   "critical" | "warning" | "info",
    "title":      short headline,
    "detail":     what was found,
    "suggestion": what to do about it,
}
"""

import hashlib

_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


def _fingerprint(url: str, layer: str, key: str) -> str:
    return hashlib.md5(f"{url}|{layer}|{key}".encode("utf-8")).hexdigest()[:12]


def _sev_from_score(score) -> str:
    if score is None:
        return "info"
    if score < 6.5:
        return "critical"
    if score < 8.0:
        return "warning"
    return "ok"


def _subscore_findings(url, layer, subscores: dict, out: list):
    """Turn {field: SubScore-dict} into findings for any scoring below 8.0."""
    for field, ss in subscores.items():
        if not isinstance(ss, dict):
            continue
        sev = _sev_from_score(ss.get("score"))
        if sev == "ok":
            continue
        suggestion = (ss.get("suggestion") or "").strip()
        if not suggestion and not (ss.get("observation") or "").strip():
            continue
        out.append({
            "id": _fingerprint(url, layer, ss.get("name") or field),
            "layer": layer,
            "severity": sev,
            "title": f"{ss.get('name', field)} · {ss.get('score')}/10",
            "detail": (ss.get("observation") or "").strip(),
            "suggestion": suggestion,
        })


def extract_findings(result: dict) -> list:
    """Flatten one PDPAnalysisResult dict into a prioritized findings list."""
    url = result.get("url", "")
    out: list = []

    # ── Copy Health: claims (compliance), brand, spelling, text insights ──
    ch = result.get("copy_health") or {}
    for cf in ch.get("claims_flags", []):
        status = (cf.get("status") or "").lower()
        if status == "ok":
            continue
        sev = "critical" if status == "flagged" else "warning"
        out.append({
            "id": _fingerprint(url, "Claims", cf.get("text", "")),
            "layer": "Claims / Compliance", "severity": sev,
            "title": cf.get("text", ""), "detail": cf.get("reason", ""), "suggestion": "",
        })
    for bf in ch.get("brand_flags", []):
        out.append({
            "id": _fingerprint(url, "Brand", bf), "layer": "Brand", "severity": "warning",
            "title": bf, "detail": "", "suggestion": "",
        })
    for err in ch.get("flagged_errors", []):
        out.append({
            "id": _fingerprint(url, "Spelling", err), "layer": "Spelling / Grammar",
            "severity": "warning", "title": err, "detail": "", "suggestion": "",
        })
    for ti in ch.get("text_insights", []):
        sev = (ti.get("severity") or "warning").lower()
        if sev == "ok":
            continue
        out.append({
            "id": _fingerprint(url, "Text", ti.get("finding", "")),
            "layer": f"Text · {ti.get('category', '')}".rstrip(" ·"),
            "severity": "critical" if sev == "critical" else "warning",
            "title": ti.get("finding", ""), "detail": ti.get("detail", ""), "suggestion": "",
        })

    # ── Persona × Narrative ──
    pn = result.get("persona_narrative") or {}
    _subscore_findings(url, "Narrative × Persona", {
        k: pn.get(k) for k in
        ("hero_banner", "carousel_flow", "banner_alignment", "page_narrative_arc", "cta_language")
    }, out)
    for fi in pn.get("flagged_issues", []):
        out.append({
            "id": _fingerprint(url, "Narrative × Persona", fi),
            "layer": "Narrative × Persona", "severity": "warning",
            "title": fi, "detail": "", "suggestion": "",
        })

    # ── Visual ──
    vd = result.get("visual_design") or {}
    _subscore_findings(url, "Visual", {
        k: vd.get(k) for k in
        ("human_presence", "proof_prominence", "ingredient_imagery", "before_after",
         "lifestyle_shots", "visual_hierarchy_brand")
    }, out)
    for fi in vd.get("flagged_issues", []):
        out.append({
            "id": _fingerprint(url, "Visual", fi), "layer": "Visual", "severity": "warning",
            "title": fi, "detail": "", "suggestion": "",
        })

    # ── Reviews ──
    rv = result.get("reviews") or {}
    _subscore_findings(url, "Reviews", {
        k: rv.get(k) for k in
        ("freshness", "rating_distribution", "theme_alignment", "negative_handling")
    }, out)
    for fi in rv.get("flagged_issues", []):
        out.append({
            "id": _fingerprint(url, "Reviews", fi), "layer": "Reviews", "severity": "warning",
            "title": fi, "detail": "", "suggestion": "",
        })

    # ── Ad Alignment (gaps carry the exact copy + placement suggestion) ──
    ad = result.get("ad_alignment") or {}
    for g in ad.get("gaps", []):
        angle = g.get("angle", "")
        out.append({
            "id": _fingerprint(url, "Ad Alignment", angle),
            "layer": "Ad Alignment", "severity": "warning",
            "title": f"Missing angle: {angle} ({g.get('conv_rate', '')})".strip(),
            "detail": g.get("what_is_missing", ""),
            "suggestion": " → ".join(x for x in [g.get("what_to_add"), g.get("where_to_add")] if x),
        })

    # ── Packaging / required-claim compliance ──
    pkg = result.get("packaging") or {}
    for rc in pkg.get("required_claims_checks", []):
        status = (rc.get("status") or "").lower()
        if status == "present":
            continue
        sev = "critical" if status in ("violated", "absent") else "warning"
        out.append({
            "id": _fingerprint(url, "Packaging", rc.get("claim", "")),
            "layer": "Packaging / Compliance", "severity": sev,
            "title": f"[{rc.get('claim_type', '')}] {rc.get('claim', '')}".strip(),
            "detail": rc.get("notes", "") or (f"Found: {rc.get('found_text')}" if rc.get("found_text") else ""),
            "suggestion": "",
        })
    _subscore_findings(url, "Packaging", {
        k: pkg.get(k) for k in ("ni_table_check", "product_photo_check", "packaging_match_check")
    }, out)

    # ── RCA (root-cause items are already the top priorities) ──
    for item in result.get("rca", []):
        out.append({
            "id": _fingerprint(url, "RCA", item.get("culprit_score", "")),
            "layer": "RCA", "severity": "critical",
            "title": f"{item.get('culprit_score', '')} · {item.get('score_value', '')}".strip(" ·"),
            "detail": " — ".join(x for x in [item.get("evidence"), item.get("why_it_matters")] if x),
            "suggestion": item.get("fix", ""),
        })

    out.sort(key=lambda f: _SEV_RANK.get(f["severity"], 9))
    return out


def summarize(findings: list) -> dict:
    """Counts by severity for a findings list."""
    c = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    c["total"] = len(findings)
    return c


# ── grouping into report-style sections ──────────────────────────────────────

# Top-level sections shown on the PDP page, in reading order. Mirrors the report
# tabs so the dashboard reads section-wise instead of one flat list.
# Sections mirror the HTML report's tabs, in the same order.
SECTION_ORDER = [
    "Hygiene Check", "Narrative × Persona", "Visual Layer", "Text Layer",
    "Reviews & Ratings", "Product Packaging", "Ad Gaps", "Root Cause",
]

_LAYER_TO_SECTION = {
    "Claims / Compliance": "Hygiene Check",     # claims accuracy
    "Brand": "Hygiene Check",                   # brand-voice violations
    "Spelling / Grammar": "Hygiene Check",      # spell/grammar
    "Narrative × Persona": "Narrative × Persona",
    "Visual": "Visual Layer",
    "Reviews": "Reviews & Ratings",
    "Packaging / Compliance": "Product Packaging",
    "Ad Alignment": "Ad Gaps",
    "RCA": "Root Cause",
}


def _section_of(layer: str) -> str:
    if layer.startswith("Text"):
        return "Text Layer"
    return _LAYER_TO_SECTION.get(layer, "Other")


def group_by_section(findings: list) -> list:
    """Group findings into ordered report-style sections with per-section counts."""
    groups: dict = {}
    for f in findings:
        groups.setdefault(_section_of(f["layer"]), []).append(f)
    ordered = []
    for name in SECTION_ORDER:
        if name in groups:
            ordered.append({"section": name, "findings": groups[name], "summary": summarize(groups[name])})
    for name, fs in groups.items():          # any unmapped sections last
        if name not in SECTION_ORDER:
            ordered.append({"section": name, "findings": fs, "summary": summarize(fs)})
    return ordered
