import re
import anthropic
from analyser.models import VisualDesignScore, SubScore, SectionFlowScore, SectionFlowIssue
from analyser.claude_client import call_claude_vision, call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from utils.config_loader import load_config
from utils.logger import get_logger

config = load_config()

log = get_logger("visual_scorer")

SYSTEM = """You are a creative director and visual strategist auditing a health/wellness PDP.
You are looking at screenshots of carousel slides and banners.
Evaluate the visual design across 6 dimensions against the persona and narrative context provided.
Be specific — describe exactly what you see and what's missing.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "overall": <float 0-10>,
  "human_presence": {
    "score": <float 0-10>,
    "observation": "<is there a human figure? does it match the persona age/lifestyle/emotion?>",
    "suggestion": "<specific fix>"
  },
  "proof_prominence": {
    "score": <float 0-10>,
    "observation": "<are clinical numbers, % stats, certifications visually prominent or buried?>",
    "suggestion": "<specific fix — size, placement, colour>"
  },
  "ingredient_imagery": {
    "score": <float 0-10>,
    "observation": "<are key ingredients shown visually? do they look premium?>",
    "suggestion": "<specific fix>"
  },
  "before_after": {
    "score": <float 0-10>,
    "observation": "<is there a before/after? is the transformation clear and believable?>",
    "suggestion": "<specific fix>"
  },
  "lifestyle_shots": {
    "score": <float 0-10>,
    "observation": "<do lifestyle images show the persona's aspirational life in real context?>",
    "suggestion": "<specific fix>"
  },
  "visual_hierarchy_brand": {
    "score": <float 0-10>,
    "observation": "<is the most important element the first thing the eye goes to? brand consistency?>",
    "suggestion": "<specific fix>"
  },
  "flagged_issues": ["<specific visual issue 1>", "<specific visual issue 2>"]
}"""


def score_visual_design(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = "",
    configured_persona: str = ""
) -> VisualDesignScore:
    log.info(f"Scoring visual design for {pdp.url}")

    # ── Collect image paths ────────────────────────────────────────────────────
    # Zeus mode: use structured ZeusImage list (labelled, full-res CDN images)
    # Playwright mode: use carousel / banner screenshot paths
    image_paths = []
    image_labels = []  # parallel list of semantic labels for the prompt

    if pdp.zeus_sourced and pdp.zeus_images:
        # Send ALL downloaded content images to Vision, in display order
        # (hero first, then content). Capped below only by max_vision_images.
        zeus_cfg = config.get("zeus", {})
        max_hero = zeus_cfg.get("max_hero_images", 9)

        hero_imgs    = [z for z in pdp.zeus_images if z.position == "hero" and z.local_path][:max_hero]
        content_imgs = [z for z in pdp.zeus_images if z.position != "hero" and z.local_path]

        for z in hero_imgs + content_imgs:
            image_paths.append(z.local_path)
            image_labels.append(f"{z.position} ({z.label or z.widget_id})")
    else:
        for slide in pdp.carousels:
            if slide.screenshot_path:
                image_paths.append(slide.screenshot_path)
                image_labels.append(f"carousel slide {slide.index}")
        for banner in pdp.banners:
            if banner.screenshot_path:
                image_paths.append(banner.screenshot_path)
                image_labels.append(f"banner ({banner.location})")

    if not image_paths:
        log.warning("No screenshots found — falling back to text-only visual analysis")
        return _text_only_fallback(client, pdp, context)

    # Cap at max_vision_images to bound API cost (default 40 — high enough to
    # cover a full PDP's content images, since chrome/nav icons are pre-filtered).
    max_vision = config.get("zeus", {}).get("max_vision_images", 40)
    if len(image_paths) > max_vision:
        log.info(f"Capping {len(image_paths)} images → {max_vision} for Vision (raise zeus.max_vision_images to send more)")
    image_paths  = image_paths[:max_vision]
    image_labels = image_labels[:max_vision]
    log.info(f"Sending {len(image_paths)} images to Claude Vision "
             f"({'Zeus CDN' if pdp.zeus_sourced else 'Playwright screenshots'})")

    image_manifest = "\n".join(
        f"  Image {i+1}: {lbl}" for i, lbl in enumerate(image_labels)
    )

    prompt = f"""PERSONA: {context.persona.name}
PERSONA TOP CONCERNS: {', '.join(context.persona.top_concerns)}
PERSONA DESCRIPTION: {context.persona.description}
NARRATIVE EMOTIONAL ARC: {context.narrative.emotional_arc}
KEY PRODUCT CLAIMS: {', '.join(context.product_brief.primary_benefits)}
KEY INGREDIENTS: {', '.join(context.product_brief.key_ingredients)}

You are viewing {len(image_paths)} images from this PDP in display order:
{image_manifest}

Evaluate all 6 visual dimensions. Be precise about what you see in each image."""

    data = call_claude_vision(client, SYSTEM, prompt, image_paths)

    # Section flow analysis — runs on text, independent of images
    flow = score_section_flow(
        client, pdp, context, configured_narrative, configured_persona
    )

    return VisualDesignScore(
        overall=data["overall"],
        human_presence=SubScore(name="Human Presence", **data["human_presence"]),
        proof_prominence=SubScore(name="Proof Prominence", **data["proof_prominence"]),
        ingredient_imagery=SubScore(name="Ingredient Imagery", **data["ingredient_imagery"]),
        before_after=SubScore(name="Before / After", **data["before_after"]),
        lifestyle_shots=SubScore(name="Lifestyle Shots", **data["lifestyle_shots"]),
        visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", **data["visual_hierarchy_brand"]),
        flagged_issues=data.get("flagged_issues", []),
        section_flow=flow,
    )


def _text_only_fallback(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext
) -> VisualDesignScore:
    """Used when screenshots are unavailable."""
    carousel_copy = "\n".join([f"Slide {s.index}: {s.copy}" for s in pdp.carousels if s.copy])
    banner_copy   = "\n".join([f"Banner ({b.location}): {b.copy}" for b in pdp.banners if b.copy])

    # No visual content at all — return a baseline placeholder rather than calling Claude with empty input
    if not carousel_copy and not banner_copy:
        log.warning("No carousel or banner text either — returning placeholder visual score")
        no_data_note = "⚠️ Visual score unavailable — no screenshots captured and no carousel/banner text found. Re-run with screenshots enabled or check CSS selectors."
        placeholder = SubScore(
            name="",
            score=5.0,
            observation="Could not be evaluated — no visual content scraped.",
            suggestion="Fix screenshot capture or CSS selectors and re-run."
        )
        return VisualDesignScore(
            overall=5.0,
            human_presence=SubScore(name="Human Presence", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            proof_prominence=SubScore(name="Proof Prominence", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            ingredient_imagery=SubScore(name="Ingredient Imagery", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            before_after=SubScore(name="Before / After", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            lifestyle_shots=SubScore(name="Lifestyle Shots", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            flagged_issues=[no_data_note]
        )

    prompt = f"""PERSONA: {context.persona.name}
PERSONA DESCRIPTION: {context.persona.description}
KEY INGREDIENTS: {', '.join(context.product_brief.key_ingredients)}

No screenshots available. Evaluate based on copy text only.
Note: scores will be limited without visual context. Do not give 0 — use 4-6 range when uncertain.

CAROUSEL TEXT: {carousel_copy or 'NONE'}
BANNER TEXT: {banner_copy or 'NONE'}"""

    data = call_claude(client, SYSTEM, prompt)

    return VisualDesignScore(
        overall=data["overall"],
        human_presence=SubScore(name="Human Presence", **data["human_presence"]),
        proof_prominence=SubScore(name="Proof Prominence", **data["proof_prominence"]),
        ingredient_imagery=SubScore(name="Ingredient Imagery", **data["ingredient_imagery"]),
        before_after=SubScore(name="Before / After", **data["before_after"]),
        lifestyle_shots=SubScore(name="Lifestyle Shots", **data["lifestyle_shots"]),
        visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", **data["visual_hierarchy_brand"]),
        flagged_issues=data.get("flagged_issues", []) + ["⚠️ Scored from text only — no screenshots available"]
    )


# ── Section order helpers ──────────────────────────────────────────────────────

# Maps Zeus widget IDs / section keys → human-readable section names
_WIDGET_LABELS = {
    "pdp-hero-slider": "Hero / Product Gallery",
    "product-summary": "Product Summary",
    "product-variants": "Product Variants",
    "narrative-facts-sugar": "Key Claims / No Sugar Narrative",
    "product-description-media-grid": "Product Description",
    "claims-banner-desktop": "Claims Banner",
    "claims-grid": "Claims Grid",
    "cocreation-image-carousel-3": "Ingredient Carousel",
    "cocreation-image-carousel-5": "Benefits Carousel",
    "cocreation-image-carousel-6": "Customer Testimonials",
    "ratings-and-reviews": "Ratings & Reviews",
    "how-we-compare": "How We Compare",
    "how-we-compare-v2": "Comparison Table",
    "top-features": "Top Features",
    "consumer-study-v2-stats": "Consumer Study / Proof Stats",
    "whats-in-the-kit": "What's in the Kit",
    "safe-and-effective-grid": "Safe & Effective",
    "things-to-note": "Things to Note",
    "how-its-used": "How to Use",
    "product-contains-details": "Product Contains / Ingredients",
    "what-it-works-best-with": "Works Best With",
    "ingredients-accordion-background": "Ingredients (Detail)",
    "faqs": "FAQ",
    "faq-accordion-show-more": "FAQ",
    "we-got-answers": "We Got Answers",
    "why-choose-mm": "Why Choose Man Matters",
    "marquee-brand": "Brand Marquee",
    "info-tile-card": "Info Tiles",
    "reel-slider-rcl": "Customer Reels",
    "customer-reviews": "Customer Reviews",
    "additional-information": "Additional Information",
    # Sections-style keys
    "clinicalProof": "Clinical Proof",
    "howItWorks": "How It Works",
    "keyIngredients": "Key Ingredients",
    "safeAndEffective": "Safe & Effective",
    "howWeCompareV2": "Comparison Table",
    "thingsToNote": "Things to Note",
    "howItsUsed": "How to Use",
    "whyChooseMM": "Why Choose Man Matters",
    "whatItWorksBestWith": "Works Best With",
    "productSwitches": "Product Variants",
    "gifComp": "Comparison Visual",
    "ingredients": "Ingredients",
    "weGotAnswers": "We Got Answers",
    "giftCallout": "Gift / Kit Callout",
    "stories": "Customer Stories",
    "consumerStudy": "Consumer Study",
    "consumerStudyV2": "Consumer Study (Stats)",
    "feelingConfusedSection": "Confused? Help Section",
    "expertsOnFingerPrints": "Expert Endorsements",
    "checkDeliveryDate": "Delivery Date Check",
    "qna": "Q&A",
    "secondaryDescription": "Product Description (Secondary)",
    "safetyIcons": "Safety Certifications",
    "videoSection": "Video",
    "mmHowToUse": "How to Use (Detail)",
    "customerJourney": "Customer Journey / Timeline",
    "additionalInformation": "Additional Information",
    # displayOrder extras
    "why-endure-data": "Why Endure / Product Proof",
    "product-equivalence-desktop": "Product Equivalence",
    "cocreation-image-carousel-3": "Ingredients Carousel",
    "cocreation-image-carousel-5": "Benefits Carousel",
    "cocreation-image-carousel-6": "Customer Testimonials",
    # growthLanding keys
    "gl_customerReview": "What Our Men Say (Before/After)",
    "gl_uses": "How to Use",
    "gl_highlights": "Product Highlights",
    "gl_caseStudy": "Case Studies",
    "gl_treats": "What It Treats",
    "gl_safeAndEffective": "Safe & Effective",
    "gl_imageGallery": "Product Gallery",
    "gl_reviewAndRating": "Ratings & Reviews",
    "gl_kitContentData": "Kit Contents",
    "gl_news": "Press / Media",
    "gl_awards": "Awards & Certifications",
    "gl_investors": "Backed By",
    "gl_product": "Product Overview",
    "gl_comparision": "Comparison",
}

# Widget IDs to skip (navigation, pricing, delivery — not content sections)
_SKIP_WIDGETS = {
    "bread-crumbs", "product-discount-tag-mobile", "product-discount-tag-desktop",
    "check-delivery-info", "wallet-discount-banner", "wallet-nudge-card",
    "installment-options", "cta-button-buy-options", "affiliate-card",
    "recently-viewed-products-slider", "frequently-bought-together-rcl",
    "first-banner-desktop", "first-banner-mobile",
    "second-banner-desktop", "second-banner-mobile",
    "third-banner-desktop", "third-banner-mobile",
    "four-banner-desktop", "four-banner-mobile",
    "marquee-brand", "reel-slider-rcl",
}


def _get_zeus_section_order(url: str) -> list:
    """
    Extract the ordered list of human-readable section names from the Zeus cache.
    Uses display_order (displayOrder-style) or sections_order (sections-style).
    Falls back to [] if no Zeus cache exists.
    """
    from scraper.zeus_connector import _page_id_from_url, _load_cache, _cache_is_stale

    page_id = _page_id_from_url(url)
    if not page_id:
        return []

    data = _load_cache(page_id)
    if not data:
        return []

    style = data.get("style", "sections")
    sections = []

    if style == "displayOrder":
        do = data.get("display_order") or {}
        # Use desktop_bottom (richest) then fall back to default
        order = do.get("desktop_bottom") or do.get("default") or []
        for widget_id in order:
            if widget_id in _SKIP_WIDGETS:
                continue
            label = _WIDGET_LABELS.get(widget_id)
            if not label:
                # Convert kebab-case widget ID to readable name
                label = widget_id.replace("-", " ").replace("_", " ").title()
                # Skip very generic names
                if label.lower() in ("cocreation image carousel 1", "cocreation image carousel 2"):
                    continue
            sections.append(label)
    else:
        # sections-style: use sections_order
        order = data.get("sections_order") or []
        for key in order:
            if key in ("order", "recentlyViewed", "frequentlyBoughtTogether"):
                continue
            label = _WIDGET_LABELS.get(key, key.replace("_", " ").replace("-", " ").title())
            sections.append(label)
        # Also add growthLanding sections if present
        for k in data.get("sections_raw", {}):
            if k.startswith("gl_") and k in _WIDGET_LABELS:
                sections.append(_WIDGET_LABELS[k])

    # Deduplicate while preserving order
    seen, unique = set(), []
    for s in sections:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    return unique


# ── Section Flow Analysis ──────────────────────────────────────────────────────

SECTION_FLOW_SYSTEM = """You are a CRO specialist analysing the section order of a health/wellness PDP.

You will receive:
- The configured narrative and target persona for this URL
- The actual section headings in current page order (top → bottom)

Your job: score how well the section sequence matches the optimal conversion narrative arc,
identify what's in the wrong position, what's missing, and what's redundant.

The optimal arc for a health product PDP:
  1. Hook / Hero — product name + core claim
  2. Problem — make the persona feel seen (pain point)
  3. Solution — introduce the product as the answer
  4. How It Works — mechanism / science (builds trust)
  5. Ingredients / What's Inside — proof of quality
  6. Social Proof — before/after, reviews, real results
  7. Clinical / Certifications — third-party validation
  8. How to Use — reduce friction
  9. Comparison / Why Us — handle objections
  10. Trust Signals — certifications, press, awards
  11. FAQ — handle remaining objections
  12. Final CTA — close

Return ONLY valid JSON — no markdown.

Schema:
{
  "score": <float 0-10>,
  "observation": "<2-3 sentences: what's working and what's broken in the current flow>",
  "current_order": ["Section Heading 1", "Section Heading 2", ...],
  "missing_sections": ["section that should exist but is absent"],
  "out_of_order": [
    {
      "section": "<heading>",
      "current_position": <int>,
      "recommended_position": <int>,
      "reason": "<why it should move — reference the persona and narrative>"
    }
  ],
  "redundant_sections": ["heading that duplicates another section's content"],
  "suggestion": "<single highest-priority reorder action — specific, actionable, persona-referenced>"
}"""


def score_section_flow(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = "",
    configured_persona: str = ""
) -> SectionFlowScore:
    """
    Analyse the order of section headings on the PDP against the optimal
    narrative arc for the configured persona and narrative.
    Returns a SectionFlowScore with reorder recommendations.
    """
    # Section order source priority:
    #   1. Live page __NEXT_DATA__ widget order (authoritative, real titles) —
    #      reliable even when the Zeus cache is stale/wrong-product.
    #   2. Zeus display/section order (widget-level, but staging can be wrong).
    #   3. Playwright-scraped H2/H3 subheads (only 3-5 on ManMatters pages).
    live_order = getattr(pdp, "live_section_order", []) or []
    zeus_order = _get_zeus_section_order(pdp.url)

    if len(live_order) >= 5:
        section_list = live_order
        source = "live page (__NEXT_DATA__)"
    elif len(zeus_order) >= 5:
        section_list = zeus_order
        source = "Zeus display order"
    else:
        section_list = pdp.subheads
        source = "Playwright H2 headings"

    if not section_list:
        log.warning(f"No section order found for {pdp.url} — skipping section flow analysis")
        return SectionFlowScore(
            score=5.0,
            observation="No section order data available — cannot analyse page flow.",
            suggestion="Check Zeus cache exists for this URL."
        )

    numbered = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(section_list))

    _persona = context.get_persona(configured_persona)
    prompt = f"""URL: {pdp.url}
CONFIGURED NARRATIVE: {configured_narrative or context.narrative.core_story}
TARGET PERSONA: {configured_persona or _persona.name}
PERSONA TOP CONCERNS: {', '.join(_persona.top_concerns[:3])}
PERSONA CORE MOTIVATION: {context.narrative.emotional_arc}

SECTIONS IN CURRENT PAGE ORDER — top to bottom ({source}):
{numbered}

Analyse whether this section sequence follows the optimal narrative arc for this persona and narrative.
Be specific — reference actual section names from the list above.
Each URL has a DIFFERENT configured narrative — tailor your analysis to "{configured_narrative or 'General'}"."""

    log.info(f"Section flow: analysing {len(section_list)} sections ({source}) for {pdp.url}")
    data = call_claude(client, SECTION_FLOW_SYSTEM, prompt)

    def _as_int(val, default=0) -> int:
        """Coerce Claude's position value to an int. Handles "3", 3.0, "1-2",
        "N/A", None — never raises (Claude doesn't always return clean ints)."""
        if isinstance(val, bool):
            return default
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            m = re.search(r"\d+", val)
            if m:
                return int(m.group())
        return default

    out_of_order = [
        SectionFlowIssue(
            section=str(item.get("section", "")),
            current_position=_as_int(item.get("current_position")),
            recommended_position=_as_int(item.get("recommended_position")),
            reason=str(item.get("reason", ""))
        )
        for item in data.get("out_of_order", [])
        if item.get("section")
    ]

    flow = SectionFlowScore(
        score=data.get("score", 5.0),
        current_order=data.get("current_order", pdp.subheads),
        missing_sections=data.get("missing_sections", []),
        out_of_order=out_of_order,
        redundant_sections=data.get("redundant_sections", []),
        observation=data.get("observation", ""),
        suggestion=data.get("suggestion", "")
    )

    log.info(
        f"Section flow scored {flow.score}/10 — "
        f"{len(out_of_order)} out-of-order, "
        f"{len(flow.missing_sections)} missing"
    )
    return flow
