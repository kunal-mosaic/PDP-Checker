"""
packaging_scorer.py — compares latest product packaging (from NPD Version Control Sheet)
against what's shown on the PDP.

Three checks per SKU:
  1. NI Table Present — is a Nutritional Information table visible on the PDP?
               Does it match the packaging?
  2. Product Photo Present — does the PDP show the actual product pack shot?
  3. Latest Packaging Match — do PDP visuals reflect the latest packaging version (V7, etc.)?

Results feed into:
  - PackagingScore (full detail for the Product Packaging tab)
"""

import base64
import io
import re
from pathlib import Path
from typing import List, Optional

import anthropic

from analyser.claude_client import call_claude, call_claude_vision_blocks
from analyser.models import (
    ClaimFlag, PackagingScore, PackagingSKUResult, RequiredClaimCheck, SubScore, VisualDesignScore
)
from scraper.version_control_connector import PackagingData, SKUPackaging
from scraper.models import PDPTextData
from utils.logger import get_logger

log = get_logger("packaging_scorer")

# ── Prompts ────────────────────────────────────────────────────────────────────

NI_SYSTEM = """You are a product compliance auditor for a health/wellness brand.
You are given:
  A) Images of the LATEST OFFICIAL PACKAGING for a product (consumer-facing artwork only)
  B) Images of the LIVE PDP (product detail page) — product pack shots only

Your job:
1. Check if a Nutritional Information (NI) table is present on the packaging artwork.
2. Check if a Nutritional Information (NI) table is present on the PDP.
3. If either is present, extract the ingredient lists from it.
4. List any discrepancies.

NOTE: The PDP only shows the product from a few angles. If the NI table is on a side/back
panel that isn't photographed on the PDP, note that — but don't flag it as a mismatch.

Return ONLY valid JSON — no markdown.

Schema:
{
  "ni_table_present_on_pdp": true | false,
  "ni_table_present_on_packaging": true | false,
  "ni_pdp_image_index": <int index of the PDP IMAGE (as labeled) that shows the NI table, or null if not found>,
  "score": <float 0-10>,
  "observation": "<ONE sentence: state if NI is present on both and whether values match>",
  "suggestion": "<ONE sentence: the single most important fix>",
  "packaging_ni": [{"ingredient": "<name>", "value": "<amount/serving>"}, ...],
  "pdp_ni": [{"ingredient": "<name>", "value": "<amount/serving>"}, ...],
  "missing_from_pdp": ["<ingredient or value on packaging but not on PDP>"],
  "extra_on_pdp": ["<ingredient or value on PDP but not on packaging>"]
}

For packaging_ni and pdp_ni: extract every ingredient/nutrient row you can read.
Include the ingredient name and its per-serving value (e.g. "300mg", "40mg", "2g").
If a value is not readable, use "?" as the value.
If values differ for the same ingredient, include in missing_from_pdp as "Ingredient: packaging says X, PDP says Y"."""


PHOTO_SYSTEM = """You are a product visual auditor.
You are given images from the LIVE PDP (product detail page).

Check if the actual product pack shot (the physical product packaging) is clearly visible on the PDP.
This means: can a customer see the real product they will receive?

Return ONLY valid JSON — no markdown.

Schema:
{
  "product_photo_present": true | false,
  "score": <float 0-10>,
  "observation": "<ONE sentence: state whether the product pack shot is visible on the PDP>",
  "suggestion": "<ONE sentence: specific fix if missing>"
}"""


PACKAGING_MATCH_SYSTEM = """You are a packaging compliance auditor.
You are given:
  A) Images of the LATEST OFFICIAL PACKAGING (from NPD drive — this is the ground truth)
  B) Images of the LIVE PDP showing the product

Compare them and identify any visual mismatches — specific elements on the PDP that differ
from the latest official packaging.

IMPORTANT — panel-aware comparison (follow these steps exactly):

The packaging artwork is often a FLAT DIELINE showing ALL panels unfolded in one image
(front panel, back panel, left side, right side, top, bottom). The PDP images show the
product from one or a few specific angles.

For each PDP image:
  Step 1: Identify which face/panel of the product is visible in this PDP image
          (e.g. "front panel", "back panel", "right side panel").
  Step 2: In the packaging artwork, locate that SAME panel.
  Step 3: Compare ONLY those matching areas. Ignore all other panels.

Do NOT flag elements from a non-matching panel. Examples:
  - If the PDP shows only the front face, do NOT flag differences in back-panel text or
    back-panel NI table as mismatches.
  - If the PDP shows front+side, compare front-to-front AND side-to-side separately.
  - Only report a mismatch when the SAME panel/face is visible on BOTH and the content differs
    (e.g. different badge, outdated claim, wrong design element on the PDP).

Additional rules:
- Technology name variants are NOT mismatches: "Cetosomes", "Cetosomal", "CETOSOMAL®", and
  "Cetosomes - Penetration Booster" all refer to the same technology. Do NOT flag spelling
  variants or trademark-style capitalisation differences (e.g. "Cetosomes" vs "CETOSOMAL®")
  as mismatches — only flag if the technology claim itself is substantively different.
- Distinguish PAGE marketing copy from ON-PACK printing. IGNORE only text/badges that sit in
  the page layout OUTSIDE the physical pack — floating bullets or captions in the margin next
  to the photo (e.g. a "Best results in 3-4 months" caption beside the product). These are
  web-page copy, not packaging.
  BUT: any text, claim, icon, or badge that is PHYSICALLY PRINTED ON THE PACK SURFACE itself
  (on the box/bottle/pouch face in the photo) IS part of the packaging and MUST be compared.
  If such an on-pack element appears on the PDP pack but is absent from the official artwork
  (or vice-versa) — e.g. a "100% Vegan" badge printed on the box front — that IS a real
  mismatch and must be flagged. When unsure whether text is on the pack or in the page margin,
  treat it as ON-PACK and compare it.

Return ONLY valid JSON — no markdown.

Schema:
{
  "matches": true | false,
  "score": <float 0-10>,
  "observation": "<ONE sentence: state whether PDP visuals match latest packaging>",
  "suggestion": "<ONE sentence: what to update on the PDP>",
  "mismatch_details": [
    {"element": "<what differs>", "on_packaging": "<what packaging shows>", "on_pdp": "<what PDP shows>"},
    ...
  ]
}

Each mismatch_details entry must have: element (what differs), on_packaging (value on packaging),
on_pdp (value on PDP or 'Not visible'). Be specific — name the exact text, badge, or design element."""


# ── Image classification (filter manufacturing diagrams / marketing content) ──

PACKAGING_CLASSIFY_PROMPT = """Classify each image into exactly ONE category:
- "consumer_packaging": Consumer-facing packaging artwork — the finished printed design
  showing branding, product name, ingredients list, nutritional info table, product photos
  on the box. What a customer would see on the shelf.
- "manufacturing_spec": Die-cut layout, dimension drawing, emboss/deboss guide, drip-off
  template, fold lines, Pantone/color specification, or any manufacturing technical document.
  These typically have dimension annotations, cut marks, thin line drawings, and technical layouts.

Return ONLY valid JSON: {"classifications": [{"index": 0, "category": "consumer_packaging"}, ...]}"""

PDP_CLASSIFY_PROMPT = """Classify each image into exactly ONE category:
- "product_shot": A photograph clearly showing the physical product — the bottle, box,
  tube, pouch, or kit contents laid out. The actual product a customer would receive.
- "marketing_content": Marketing banner, lifestyle photo, testimonial, before/after
  comparison, infographic, chart, educational content, or text-heavy promotional image
  that is NOT a direct photo of the product itself.

Return ONLY valid JSON: {"classifications": [{"index": 0, "category": "product_shot"}, ...]}"""


def _classify_images(
    client: anthropic.Anthropic,
    images: list,
    classify_prompt: str,
) -> dict:
    """Send images to Claude for classification. Returns {index: category}."""
    if not images:
        return {}
    content = []
    for i, img in enumerate(images):
        content.append({"type": "text", "text": f"IMAGE {i}"})
        content.append(img)
    content.append({"type": "text", "text": classify_prompt})
    try:
        data = call_claude_vision_blocks(
            client, "You classify product images into categories.",
            content, model="claude-sonnet-4-6",
        )
        return {c.get("index", -1): c.get("category", "") for c in data.get("classifications", [])}
    except Exception as e:
        log.warning("Image classification failed: %s -- using all images", e)
        return {}


def _match_pdp_images_to_sku(
    client: anthropic.Anthropic,
    pdp_images: list,
    sku_name: str,
) -> tuple:
    """
    Filter PDP images to only those showing a specific SKU product.
    Returns (filtered_images, only_cluttered_shots, matched_ok):
    - filtered_images: the images to compare against this SKU's packaging
    - only_cluttered_shots: True when every match was a cluttered kit/lifestyle shot (no clean
      single-product shot), so the caller can tell the match step to be conservative
    - matched_ok: True when the matcher ran and produced a determination. False when the
      matcher CALL FAILED (e.g. JSON parse error / API flake). The caller must treat
      matched_ok=False differently from an empty match: on failure, fall back to the input
      gallery images rather than showing "No PDP product images" — the input is already the
      hero/carousel gallery, so it's a safe fallback, unlike raw all-images.
    """
    if not pdp_images or len(pdp_images) <= 1:
        return pdp_images, False, True
    content = []
    for i, img in enumerate(pdp_images):
        content.append({"type": "text", "text": f"IMAGE {i}"})
        content.append(img)
    prompt = f"""Which of these images show the SPECIFIC product "{sku_name}"?

CRITICAL: Man Matters sells many different products that all share the same brand look —
same navy/white colours, same "man matters" logo, same typography. Brand resemblance is NOT
a match. You must identify the product by its ACTUAL NAME and FORM printed on the pack
(e.g. is it a "Hair Gummies" box vs a "Minoxidil" bottle vs a "Shilajit Gummies" jar), not by
colour or logo. When unsure, choose "other" rather than guessing "match".

For each image, decide:
- "match": The actual "{sku_name}" pack is visibly present — you can identify it by its printed
  product name / form, even if it appears alongside other products in a kit/combo shot
- "other": "{sku_name}" is not present, OR the image only shows a DIFFERENT Man Matters product
  that merely looks similar (same branding/colour but a different product name or form)

For each "match", also rate clarity:
- "clear": "{sku_name}" is the main subject, unobstructed, large enough to read its label
- "cluttered": "{sku_name}" appears but is small, partially obscured, or one of several products in a kit/lifestyle shot

Return ONLY valid JSON: {{"matches": [{{"index": 0, "result": "match", "clarity": "clear"}}, ...]}}"""
    content.append({"type": "text", "text": prompt})
    for attempt in range(2):
        try:
            data = call_claude_vision_blocks(
                client, "You match product images to specific SKUs.",
                content, model="claude-sonnet-4-6",
            )
            match_entries = [m for m in data.get("matches", []) if m.get("result") == "match"]
            if not match_entries:
                # Genuine "not sold on this PDP" result — do NOT fall back to all images.
                # Falling back here caused false packaging mismatches (e.g. comparing a
                # Minoxidil bottle against a gummies-only PDP that never shows it).
                log.info("      SKU match: '%s' not visible on this PDP -- skipping (not a fallback)", sku_name)
                return [], False, True
            # Prefer clear, unobstructed single-product shots over cluttered kit/lifestyle
            # shots — a packaging comparison against a small, partially-hidden pack in a
            # kit photo produces unreliable mismatches.
            match_entries.sort(key=lambda m: 0 if m.get("clarity") == "clear" else 1)
            only_cluttered = not any(m.get("clarity") == "clear" for m in match_entries)
            matched = [m["index"] for m in match_entries][:5]  # cap so clear shots dominate the prompt
            filtered = [img for i in matched for j, img in enumerate(pdp_images) if j == i]
            if len(filtered) < len(pdp_images):
                log.info("      SKU match: kept %d/%d PDP images for '%s' (clear shots prioritised)", len(filtered), len(pdp_images), sku_name)
            if only_cluttered:
                log.info("      SKU match: only cluttered/kit shots found for '%s' -- no clean shot available", sku_name)
            return filtered, only_cluttered, True
        except Exception as e:
            if attempt == 0:
                log.warning("SKU image matching failed (attempt 1): %s -- retrying", e)
            else:
                log.warning("SKU image matching failed after retry: %s -- falling back to gallery images", e)
    # Both attempts failed to PARSE (API flake / truncated JSON) — this is NOT a "product
    # absent" result, so we must not return empty (that shows "No PDP product images").
    # matched_ok=False tells the caller to fall back to the input gallery, which is already
    # the hero/carousel product gallery (cross-sell excluded upstream) — a safe fallback,
    # unlike the old "all raw images" fallback that pulled in other products.
    return pdp_images, False, False


def _filter_by_category(
    images: list,
    classifications: dict,
    keep_category: str,
    label: str,
) -> list:
    """Filter images by classification category, with fallback to all."""
    if not classifications:
        return images
    filtered = [img for i, img in enumerate(images)
                if classifications.get(i) == keep_category]
    if not filtered:
        log.warning("No images classified as '%s' -- keeping all %s images", keep_category, label)
        return images
    dropped = len(images) - len(filtered)
    if dropped:
        log.info(f"      Filtered {label}: kept {len(filtered)}/{len(images)} ({keep_category})")
    return filtered


# ── Image loading ──────────────────────────────────────────────────────────────

def _load_images_as_b64(paths: List[Path], max_images: int = 5) -> List[dict]:
    """Convert local image/PDF files to base64 blocks for Claude Vision."""
    from PIL import Image

    result = []
    for path in paths[:max_images]:
        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                import fitz
                doc = fitz.open(str(path))
                for page_num in range(min(6, len(doc))):
                    page = doc[page_num]
                    pix = page.get_pixmap(dpi=200)
                    # PDF pages at 200dpi routinely exceed Claude's 2000px multi-image
                    # limit (e.g. A4 ≈ 1654x2339px) — resize the same way the plain-image
                    # path below already does, or the request gets rejected outright.
                    page_img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                    page_img.thumbnail((1200, 1200))
                    buf = io.BytesIO()
                    page_img.save(buf, format="JPEG", quality=85)
                    result.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(buf.getvalue()).decode(),
                        }
                    })
                doc.close()
            elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                img = Image.open(path).convert("RGB")
                img.thumbnail((1200, 1200))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(buf.getvalue()).decode(),
                    }
                })
        except Exception as e:
            log.warning(f"Could not load image {path.name}: {e}")

    return result


def _extract_b64_strings(blocks: List[dict], max_items: int = 3) -> List[str]:
    """Pull raw base64 strings from Claude Vision content blocks (for embedding in report)."""
    out = []
    for b in blocks:
        if b.get("type") == "image" and len(out) < max_items:
            out.append(b["source"]["data"])
    return out


# Image sections that show OTHER products, never the one being audited — always excluded
# from packaging checks (else a "frequently bought together" Multivitamin jar gets compared
# against the Shilajit artwork, etc.)
_CROSS_SELL_RE = re.compile(
    r"frequent|bought.?together|you.?may|also.?like|recommend|upsell|cross.?sell|"
    r"similar|recently.?viewed|complete.?the|pairs?.?with|customers?.?also",
    re.I,
)

# Priority for the packaging-match step: the main product gallery the customer sees first.
# hero + carousel are the top image strip (where the real pack shots live), then sections,
# then marketing banners last.
_IMG_TYPE_PRIORITY = {"hero": 0, "carousel": 1, "section": 2, "banner": 3}


def _is_cross_sell(img) -> bool:
    """True if an image belongs to a cross-sell / recommendation widget (other products)."""
    hay = " ".join([
        getattr(img, "label", "") or "",
        getattr(img, "widget_id", "") or "",
        getattr(img, "position", "") or "",
        getattr(img, "widget_type", "") or "",
    ])
    return bool(_CROSS_SELL_RE.search(hay))


def _img_priority(img) -> int:
    return _IMG_TYPE_PRIORITY.get(getattr(img, "image_type", "") or "", 4)


def _load_pdp_images(pdp: PDPTextData, max_images: int = 8, gallery_only: bool = False) -> List[dict]:
    """
    Load PDP images for packaging checks.

    - Always drops testimonials, comparisons, and cross-sell / "frequently bought together"
      images — those show OTHER products and cause false packaging comparisons.
    - Orders hero + main carousel first (the top gallery the customer sees), so the best
      product shots are used before falling back to lower page sections.
    - gallery_only=True keeps only product-gallery types (hero/carousel/section, plus
      untyped) and drops marketing banners — used for the packaging-match step (metadata
      path "A"). Leave False for the NI-table scan, which needs section/infographic slides.
    """
    skip_types = {"testimonial", "comparison"}
    candidates = []
    for img in pdp.zeus_images:
        if img.image_type in skip_types:
            continue
        if _is_cross_sell(img):
            continue
        if gallery_only and (img.image_type or "") not in ("hero", "carousel", "section", ""):
            continue
        local = getattr(img, "local_path", None)
        if local and Path(local).exists():
            candidates.append(img)

    # Stable sort keeps original order within each priority tier (so hero slide 1 stays
    # before hero slide 2), while lifting the product gallery above marketing banners.
    candidates.sort(key=_img_priority)

    images = []
    for img in candidates:
        loaded = _load_images_as_b64([Path(img.local_path)], max_images=1)
        images.extend(loaded)
        if len(images) >= max_images:
            break
    return images


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score_sku(
    client: anthropic.Anthropic,
    sku: SKUPackaging,
    pdp: PDPTextData,
    filtered_pdp_images: Optional[List[dict]] = None,
    all_pdp_images: Optional[List[dict]] = None,
) -> PackagingSKUResult:
    """Run all 3 checks for a single SKU."""
    raw_packaging = _load_images_as_b64(sku.local_files, max_images=10)

    # Classify packaging images — keep only consumer-facing artwork, drop manufacturing diagrams
    pkg_classes = _classify_images(client, raw_packaging, PACKAGING_CLASSIFY_PROMPT)
    packaging_images = _filter_by_category(raw_packaging, pkg_classes, "consumer_packaging", "packaging")

    # product_shot images — used for packaging match (panel-aware) and product photo check
    pdp_images = filtered_pdp_images if filtered_pdp_images is not None else _load_pdp_images(pdp)

    # ALL PDP images (unfiltered) — used for NI table check so infographic/educational slides are included
    all_pdp_images_for_ni = all_pdp_images if all_pdp_images is not None else _load_pdp_images(pdp, max_images=24)

    # Per-SKU filtering: keep only PDP images that show THIS specific product
    had_images_before_sku_filter = len(pdp_images) > 1
    only_cluttered_shots = False
    matched_ok = True
    if had_images_before_sku_filter:
        pdp_images, only_cluttered_shots, matched_ok = _match_pdp_images_to_sku(client, pdp_images, sku.sku_name)
    # "Genuinely not on this PDP" only when the matcher actually RAN and returned nothing.
    # If the matcher call failed (matched_ok=False), pdp_images was left as the gallery
    # fallback — treat it as a normal comparison, not as "not present".
    sku_not_on_this_pdp = had_images_before_sku_filter and matched_ok and len(pdp_images) == 0

    has_packaging = len(packaging_images) > 0
    has_pdp_images = len(pdp_images) > 0

    result = PackagingSKUResult(
        sku_name=sku.sku_name,
        version=sku.version,
        drive_folder_url=sku.drive_folder_url,
    )

    # Save base64 images for report embedding
    result.packaging_images_b64 = _extract_b64_strings(packaging_images, max_items=2)
    result.pdp_product_images_b64 = _extract_b64_strings(pdp_images, max_items=2)

    # Softer than "not sold on this PDP" — many PDPs sell multiple pack-count/quantity
    # variants (e.g. 30s vs 60s) through a quantity selector that does NOT swap the product
    # photo, so there may be no image on the page that visually distinguishes this specific
    # SKU from a sibling variant. That's a photography limitation, not proof the SKU is absent.
    not_applicable_msg = (
        f"No PDP image could be matched specifically to '{sku.sku_name}' — this page may use "
        f"the same product photo for multiple quantity/variant options, so this SKU's own pack "
        f"may not be visually distinguishable here even if it IS sold on this page."
    )
    if sku_not_on_this_pdp:
        log.info(f"      {not_applicable_msg}")
    elif not has_pdp_images:
        log.warning(f"      No PDP images available for {sku.sku_name}")

    # ── Check 1: Product photo present ────────────────────────────────────────
    if has_pdp_images:
        photo_data = call_claude_vision_blocks(
            client,
            PHOTO_SYSTEM,
            [*pdp_images, {"type": "text", "text": "Check if the product pack shot is visible on this PDP."}],
        )
        result.product_photo_present = photo_data.get("product_photo_present", False)
    elif sku_not_on_this_pdp:
        photo_data = {"score": None, "observation": not_applicable_msg, "suggestion": ""}
    else:
        photo_data = {"score": 0.0, "observation": "No PDP images available", "suggestion": "Scrape PDP visuals"}

    # ── Check 2: NI Table (PDP vs packaging) ──────────────────────────────────
    # Use ALL PDP images here — NI table appears on infographic/educational slides,
    # not on product pack shots. Filtering to product_shot would hide the NI card.
    has_all_pdp = len(all_pdp_images_for_ni) > 0
    if has_all_pdp:
        ni_content_blocks = []
        if has_packaging:
            ni_content_blocks.append({"type": "text", "text": "--- OFFICIAL PACKAGING IMAGES BELOW ---"})
            ni_content_blocks.extend(packaging_images)
        ni_content_blocks.append({"type": "text", "text": "--- LIVE PDP IMAGES BELOW (all slides including infographics and educational cards) ---"})
        for i, img in enumerate(all_pdp_images_for_ni):
            ni_content_blocks.append({"type": "text", "text": f"PDP IMAGE {i}"})
            ni_content_blocks.append(img)

        ni_content_blocks.append({"type": "text", "text": "Look through ALL PDP images above for any nutrition/ingredient table — including infographic slides and formulation cards, not just product pack shots. Compare the NI table on the packaging vs the PDP. Extract ingredient lists from both. Report which PDP IMAGE index shows the NI table."})
        ni_data = call_claude_vision_blocks(client, NI_SYSTEM, ni_content_blocks)
        result.ni_table_present = ni_data.get("ni_table_present_on_pdp", False)
        result.ni_table_matches = ni_data.get("ni_table_present_on_packaging") and ni_data.get("ni_table_present_on_pdp")
        result.ni_table_diff = (
            [f"Missing from PDP: {x}" for x in ni_data.get("missing_from_pdp", [])] +
            [f"Extra on PDP: {x}" for x in ni_data.get("extra_on_pdp", [])]
        )
        ni_img_idx = ni_data.get("ni_pdp_image_index")
        if isinstance(ni_img_idx, int) and 0 <= ni_img_idx < len(all_pdp_images_for_ni):
            result.ni_table_pdp_image_b64 = all_pdp_images_for_ni[ni_img_idx]["source"]["data"]
        result.ni_packaging_values = ni_data.get("packaging_ni", [])
        result.ni_pdp_values = ni_data.get("pdp_ni", [])
    else:
        ni_data = {"score": 0.0, "observation": "No PDP images available", "suggestion": "Scrape PDP visuals"}

    # ── Check 3: Latest packaging match ──────────────────────────────────────
    if has_packaging and has_pdp_images:
        visible_panels = _identify_pdp_panels(client, pdp_images)
        log.info("      Panel identification: %s", visible_panels)
        panel_instruction = (
            f"The PDP images show the {visible_panels} panel(s) of the product. "
            f"If the packaging artwork is a flat dieline, compare ONLY the {visible_panels} panel area(s). "
            f"Do NOT flag differences from other panels (back, side, top, bottom) that are not visible on the PDP."
        )
        clutter_caveat = (
            f"\nCAVEAT: No clean, unobstructed shot of '{sku.sku_name}' alone was found on this PDP — "
            f"only images where it appears small, partially hidden, or alongside other products in a "
            f"kit/lifestyle photo. Be conservative: only report a mismatch if you can clearly read the "
            f"element on both sides. Do NOT flag differences you're inferring from a small/unclear view."
            if only_cluttered_shots else ""
        )
        match_blocks = [
            {"type": "text", "text": f"--- OFFICIAL PACKAGING IMAGES (latest version) ---\nPANEL SCOPE: {panel_instruction}{clutter_caveat}"},
            *packaging_images,
            {"type": "text", "text": "--- LIVE PDP IMAGES ---"},
            *pdp_images,
            {"type": "text", "text": f"Compare packaging vs PDP. {panel_instruction}{clutter_caveat}"},
        ]
        match_data = call_claude_vision_blocks(client, PACKAGING_MATCH_SYSTEM, match_blocks)
        result.packaging_match = match_data.get("matches", None)
        result.packaging_mismatch_details = match_data.get("mismatch_details", [])
        if only_cluttered_shots:
            result.packaging_mismatch_details = [
                {**d, "on_pdp": f"{d.get('on_pdp','')} (⚠ only a cluttered/kit shot was available for this SKU)"}
                for d in result.packaging_mismatch_details
            ]
    elif sku_not_on_this_pdp:
        match_data = {"score": None, "observation": not_applicable_msg, "suggestion": ""}
    else:
        match_data = {
            "score": 0.0,
            "observation": "Packaging files not available" if not has_packaging else "No PDP images",
            "suggestion": "Upload latest packaging to Drive" if not has_packaging else "",
        }

    # Store sub-data on result for parent aggregation
    result._photo_data = photo_data
    result._ni_data = ni_data
    result._match_data = match_data

    return result


REQUIRED_CLAIMS_SYSTEM = """You are a product compliance auditor for a health/wellness brand.
You are given:
  A) The full text of a live PDP + ALL PDP images (carousels, banners, product shots,
     educational slides, infographic cards, ingredient tables — everything on the page)
  B) LATEST APPROVED LABEL images from the brand's Drive folder — treat these as ground truth

Use BOTH the PDP content AND the label images to determine each check's status.
For "verify X matches latest label" checks: read the value from the label image, read it from the PDP, then compare.

For each check, determine its status:
- "present":        the required claim / info is found on the PDP (text or image)
- "absent":         the required claim is NOT found anywhere on the PDP
- "violated":       a PROHIBITED claim IS found on the PDP (text or image) — this is bad
- "mismatch":       a value is present on PDP but does NOT match the latest label
- "cannot_verify":  the label image is unclear/unreadable AND the PDP content is ambiguous

HARD RULE — status and notes must never contradict each other:
- Only return "present" or "violated" if you can quote the EXACT text/image location where the
  claim was found, in the "found_text" field. If you cannot point to a specific location, the
  status MUST be "absent" — never "present" or "violated" for something you did not actually find.
- If your notes say "not found" / "not visible" / "does not appear", the status MUST be "absent",
  not "violated" or "present". Re-check your own answer before returning it: does the status match
  what the notes say? If not, fix the status to match the notes.

CRITICAL for prohibited claims: scan BOTH the text AND every image carefully for the banned phrase.
If a prohibited phrase appears as a badge, icon label, or graphic text in any image, or anywhere
in FAQ / additional info text → status = "violated". Do not miss text embedded in images.

CRITICAL for NI table checks: Actively scan EVERY PDP image — including infographic slides,
educational cards, and formulation cards — not just product pack shots. The NI table is often
shown as a dedicated "Clean & Effective Formulation" or "Nutrition Information" image card.
- If the claim is "NI table must be present": mark "present" if ANY PDP image shows a
  nutrition/ingredient table. Mark "absent" only if NO image contains such a table.
- When the NI table is found: compare every row against the LABEL image. For each row that
  differs (wrong value, missing ingredient, extra ingredient), report it as a separate check
  entry with status "mismatch" and notes stating label value vs PDP value.

For manufacturer / serving size / ingredient / dosage checks: read the label image first, then
compare to PDP. If they match → "present". If they differ → "mismatch" with both values in notes.
Only use "cannot_verify" if the label image truly cannot be read.

Be lenient with wording for required claims — synonyms count.

Return ONLY valid JSON — no markdown.

Schema:
{
  "checks": [
    {
      "claim": "<exact claim text from the required check>",
      "claim_type": "<required|prohibited|manufacturer|ni_value>",
      "status": "<present|absent|violated|mismatch|cannot_verify>",
      "found_text": "<exact snippet or image description where this was found, or null>",
      "notes": "<brief explanation>"
    }
  ]
}"""


PANEL_ID_SYSTEM = """Look at these product images from a live PDP. For each image, identify which face/panel of the product is visible.

Return ONLY valid JSON — no markdown.
Schema: {"panels": [{"index": 0, "panel": "front"}, ...]}
Panel options: "front", "back", "side", "top", "bottom", "multiple", "unclear"

"front" = the main branded face customers see on the shelf (product name, logo, key claims).
"back" = the panel with ingredient list, nutritional information table, manufacturer details.
"multiple" = image shows both front and back/side panels simultaneously."""


def _identify_pdp_panels(client: anthropic.Anthropic, pdp_images: list) -> str:
    """Identify which panels are visible across PDP images. Returns a human-readable summary."""
    if not pdp_images:
        return "front"
    content = []
    for i, img in enumerate(pdp_images[:6]):
        content.append({"type": "text", "text": f"IMAGE {i}"})
        content.append(img)
    content.append({"type": "text", "text": PANEL_ID_SYSTEM})
    try:
        data = call_claude_vision_blocks(
            client, "You identify product packaging panel orientations.",
            content, model="claude-sonnet-4-6",
        )
        panels = {p.get("panel", "unclear") for p in data.get("panels", [])}
        panels.discard("unclear")
        return ", ".join(sorted(panels)) if panels else "front"
    except Exception as e:
        log.warning("Panel identification failed: %s — assuming front", e)
        return "front"


def _pdp_text_for_claims(pdp: PDPTextData, head_chars: int = 8000) -> str:
    """
    Return PDP text for the claims check WITHOUT losing the bottom-of-page
    "Additional Information" block (Manufactured By, Marketed By, FSSAI, Net Quantity,
    Country of Origin). That block sits at the very end of the page, so a plain
    full_page_text[:8000] truncation silently drops it — which made "Manufactured by"
    always come back CANNOT_VERIFY even though every PDP lists it.

    Takes the head of the page, then always appends any windows around manufacturer /
    additional-info keywords found beyond the cutoff.
    """
    full = pdp.full_page_text or ""
    text = full[:head_chars]
    if len(full) <= head_chars:
        return text

    tail = full[head_chars:]
    kw = re.compile(
        r"manufactured\s+by|marketed\s+by|additional\s+information|"
        r"country\s+of\s+origin|fssai|net\s+quantity|best\s+before",
        re.I,
    )
    windows = []
    last_end = -1
    for m in kw.finditer(tail):
        start = max(0, m.start() - 200)
        end = min(len(tail), m.end() + 400)
        if start <= last_end:  # merge overlapping windows
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
        last_end = end
    if windows:
        snippet = "\n...\n".join(tail[s:e] for s, e in windows[:8])[:4000]
        text += "\n\n--- ADDITIONAL INFORMATION (from lower on the page) ---\n" + snippet
    return text


def _check_required_claims(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    required_claims,
    label_images: list = None,
) -> "List[RequiredClaimCheck]":
    """Check PDP text + images against required claims, using Drive label images as reference."""
    from ingester.models import RequiredClaimsContext
    if not required_claims or not isinstance(required_claims, RequiredClaimsContext):
        return []

    checks_list = []
    for c in required_claims.health_claims_required:
        checks_list.append(f'[required] {c}')
    for c in required_claims.prohibited_claims:
        checks_list.append(f'[prohibited] {c}')
    if required_claims.manufacturer:
        checks_list.append(f'[manufacturer] {required_claims.manufacturer}')
    for row in required_claims.ni_table:
        checks_list.append(f'[ni_value] {row.ingredient}: {row.value}')
    for c in required_claims.additional_checks:
        checks_list.append(f'[required] {c}')

    if not checks_list:
        return []

    pdp_text = _pdp_text_for_claims(pdp)
    # Load ALL PDP images (no type filter) — NI tables appear on educational/infographic slides,
    # which can sit anywhere in the carousel/banner order, so the cap must be high enough that
    # a table slide near the end of a long PDP isn't silently dropped before Claude sees it.
    pdp_images = _load_pdp_images(pdp, max_images=24)

    content = [
        {
            "type": "text",
            "text": (
                f"REQUIRED COMPLIANCE CHECKS:\n{chr(10).join(checks_list)}\n\n"
                f"PDP TEXT CONTENT:\n{pdp_text}"
            ),
        }
    ]
    if label_images:
        content.append({"type": "text", "text": "--- LATEST APPROVED LABEL IMAGES (ground truth — use these to verify values) ---"})
        content.extend(label_images[:4])
    if pdp_images:
        content.append({"type": "text", "text": "--- LIVE PDP IMAGES (what customers currently see) ---"})
        content.extend(pdp_images)
    content.append({"type": "text", "text": "Check every item above. For verify/match checks, read the value from the label images and compare to PDP. Return the JSON."})

    try:
        data = call_claude_vision_blocks(client, REQUIRED_CLAIMS_SYSTEM, content)
        results = [RequiredClaimCheck(**c) for c in data.get("checks", [])]
        # Normalise: prohibited claim found on PDP = violation, not "present"
        for r in results:
            if r.claim_type == "prohibited" and r.status == "present":
                r.status = "violated"
        return results
    except Exception as e:
        log.warning("Required claims check failed: %s", e)
        return []


def required_claims_to_claim_flags(checks: "List[RequiredClaimCheck]") -> "List[ClaimFlag]":
    """
    Convert MasterDoc-driven RequiredClaimCheck results into ClaimFlag entries so they
    render in the Hygiene Check → Claims tab (Flagged / Warning / Verified), alongside
    the brief-vs-PDP accuracy check. This is the single masterdoc-driven source for GM
    compliance and email-specific checks — there is no separate hardcoded GM scan.
    """
    flags = []
    for c in checks:
        if c.status in ("violated", "mismatch"):
            status = "flagged"
        elif c.status == "cannot_verify":
            status = "warning"
        elif c.status == "absent" and c.claim_type != "prohibited":
            # A required item that's missing is a real gap — flag it, not just a warning
            status = "flagged"
        elif c.status == "absent" and c.claim_type == "prohibited":
            status = "ok"  # correctly NOT present — compliant
        else:  # "present" for a required/manufacturer/ni_value claim
            status = "ok"
        flags.append(ClaimFlag(text=c.claim, status=status, reason=c.notes or c.found_text or ""))
    return flags


def score_packaging(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    packaging_data: PackagingData,
    skip_pdp_prefilter: bool = False,
    required_claims=None,
) -> PackagingScore:
    """
    Score packaging for all SKUs in a product.
    Returns PackagingScore with full detail + 3 aggregated sub-scores.
    """
    if packaging_data.error:
        log.warning(f"Packaging data error: {packaging_data.error}")
        return PackagingScore(error=packaging_data.error)

    if not packaging_data.skus:
        return PackagingScore(error="No SKUs in packaging data")

    # Load PDP images once (shared across all SKUs).
    # raw_pdp: all product-relevant images (cross-sell/testimonials already excluded),
    #          hero-first — used for the NI-table scan which needs section/infographic slides.
    raw_pdp = _load_pdp_images(pdp, max_images=24)

    if skip_pdp_prefilter:
        filtered_pdp = raw_pdp
        log.info("      PDP pre-filter skipped -- passing all %d images to per-SKU matcher", len(raw_pdp))
    else:
        # A (primary): metadata path — the main product gallery (hero + carousel + sections),
        # marketing banners dropped, cross-sell already excluded. Composite bottle+box shots
        # survive here so the per-SKU matcher can isolate the right product from them.
        filtered_pdp = _load_pdp_images(pdp, max_images=24, gallery_only=True)
        if filtered_pdp:
            log.info("      PDP images (A/metadata): %d gallery shots (hero-first)", len(filtered_pdp))
        else:
            # B (fallback): metadata gave us nothing usable — ask Claude Vision which raw
            # images are genuine product shots.
            log.info("      PDP metadata path empty -- falling back to vision classify (B)")
            pdp_classes = _classify_images(client, raw_pdp, PDP_CLASSIFY_PROMPT)
            filtered_pdp = _filter_by_category(raw_pdp, pdp_classes, "product_shot", "PDP")

    sku_results = []
    photo_scores, ni_scores, match_scores = [], [], []
    photo_obs, ni_obs, match_obs = [], [], []

    for sku in packaging_data.skus:
        if not sku.drive_folder_url:
            log.info("      Skipping %s -- no Drive link", sku.sku_name)
            sku_results.append(PackagingSKUResult(
                sku_name=sku.sku_name,
                version=sku.version,
                drive_folder_url="",
            ))
            continue

        log.info(f"      Scoring packaging: {sku.sku_name} ({sku.version})")
        scored = _score_sku(client, sku, pdp, filtered_pdp_images=filtered_pdp, all_pdp_images=raw_pdp)
        sku_results.append(scored)

        photo_d = getattr(scored, "_photo_data", {})
        ni_d = getattr(scored, "_ni_data", {})
        match_d = getattr(scored, "_match_data", {})

        if photo_d.get("score") is not None:
            photo_scores.append(float(photo_d.get("score", 0)))
            photo_obs.append(photo_d.get("observation", ""))
        if ni_d.get("score") is not None:
            ni_scores.append(float(ni_d.get("score", 0)))
            ni_obs.append(ni_d.get("observation", ""))
        if match_d.get("score") is not None:
            match_scores.append(float(match_d.get("score", 0)))
            match_obs.append(match_d.get("observation", ""))

    def _avg(lst): return round(sum(lst) / len(lst), 1) if lst else 0.0
    def _join_obs(lst): return " | ".join(o for o in lst if o) or "No data"

    photo_score_val = _avg(photo_scores)
    ni_score_val = _avg(ni_scores)
    match_score_val = _avg(match_scores)
    overall = round((photo_score_val + ni_score_val + match_score_val) / 3, 1) if (photo_scores or ni_scores or match_scores) else 0.0

    # Collect label images from all SKUs for the required claims check
    all_label_images = []
    for sr in sku_results:
        if sr.packaging_images_b64:
            for b64 in sr.packaging_images_b64[:2]:
                all_label_images.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                })

    claims_checks = _check_required_claims(client, pdp, required_claims, label_images=all_label_images or None)
    if claims_checks:
        log.info("      Required claims: %d checks completed", len(claims_checks))

    return PackagingScore(
        overall=overall,
        ni_table_check=SubScore(
            name="NI Table",
            score=ni_score_val,
            observation=_join_obs(ni_obs),
            suggestion="Ensure NI table matches latest packaging and is visible on PDP.",
        ),
        product_photo_check=SubScore(
            name="Product Photo",
            score=photo_score_val,
            observation=_join_obs(photo_obs),
            suggestion="Ensure the actual product pack shot is prominently displayed.",
        ),
        packaging_match_check=SubScore(
            name="Latest Packaging Match",
            score=match_score_val,
            observation=_join_obs(match_obs),
            suggestion="Update PDP images to reflect the latest approved packaging version.",
        ),
        sku_results=sku_results,
        required_claims_checks=claims_checks,
    )


def apply_packaging_to_visual_score(
    visual: VisualDesignScore,
    packaging: PackagingScore,
) -> VisualDesignScore:
    """
    Merges packaging sub-scores into the VisualDesignScore.
    These are stored on the model but no longer rendered in the Visual Layer tab
    (moved to Product Packaging tab only).
    """
    visual.ni_table_present = packaging.ni_table_check
    visual.product_photo_present = packaging.product_photo_check
    visual.latest_packaging_match = packaging.packaging_match_check
    return visual
