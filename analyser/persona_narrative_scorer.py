import anthropic
from typing import List
from analyser.models import PersonaNarrativeScore, PersonaMatrixRow, SubScore
from analyser.claude_client import call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("persona_narrative_scorer")
config = load_config()


# ── URL-level persona/narrative resolver ──────────────────────────────────────

def _resolve_url_context(
    url: str,
    configured_narrative: str,
    configured_persona: str,
    context: IngestedContext,
) -> dict:
    """
    Resolve what persona pain points and narrative pillars to use
    for THIS specific URL. Never falls back to product-level defaults silently.

    Returns:
        persona_name       - the persona assigned to this URL
        pain_points        - top concerns for this persona
        narrative_label    - the narrative name for this URL
        narrative_pillars  - pillars relevant to this narrative
        narrative_core     - one-line story arc
    """
    # Persona resolution — look up the SPECIFIC named persona for this URL (not just its
    # label) when the masterdoc describes more than one, e.g. Shilajit's Tejas/Aakash/Fitness
    # Buyer or Beard Growth Kit's Patcher/First-Time Grower/Late Resolver. Previously this only
    # swapped the display name while every URL scored against one shared generic persona.
    persona = context.get_persona(configured_persona)
    persona_name = (configured_persona or persona.name).strip()
    pain_points = list(persona.top_concerns)
    persona_motivations = list(persona.motivations)
    persona_objections = list(persona.objections)
    persona_language_cues = list(persona.language_cues)

    # Narrative resolution — use configured label; filter pillars if possible
    narrative_label = (configured_narrative or context.narrative.core_story).strip()
    all_pillars = list(context.narrative.pillars)

    # Try to filter pillars to those that match the configured narrative keyword
    # e.g. "Summer" → keep pillars containing "summer", "season", "heat", etc.
    nav_lower = narrative_label.lower()
    filtered_pillars = [
        p for p in all_pillars
        if any(word in p.lower() for word in nav_lower.split())
    ]
    narrative_pillars = filtered_pillars if filtered_pillars else all_pillars

    log.info(
        f"P×N context resolved for {url}:\n"
        f"  persona_used    = {persona_name}\n"
        f"  narrative_used  = {narrative_label}\n"
        f"  pain_points     = {pain_points[:3]}\n"
        f"  pillars (total={len(all_pillars)}, filtered={len(narrative_pillars)}): "
        f"{narrative_pillars[:3]}"
    )

    return {
        "persona_name":      persona_name,
        "pain_points":       pain_points,
        "persona_motivations":   persona_motivations,
        "persona_objections":    persona_objections,
        "persona_language_cues": persona_language_cues,
        "narrative_label":   narrative_label,
        "narrative_pillars": narrative_pillars,
        "narrative_core":    context.narrative.core_story,
    }

SYSTEM = """You are a brand strategist and conversion copywriter auditing a health/wellness PDP.
You will be given:
  - The CONFIGURED narrative this PDP is meant to target
  - The list of all product personas
  - The full PDP copy

Your job has TWO parts:

PART 1 — Score how well the page executes the configured narrative (5 dimensions).
PART 2 — For EACH persona, identify what the current PDP does RIGHT and what is MISSING
         to serve that persona, given the configured narrative.

Be specific and quote actual copy where relevant.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "overall": <float 0-10>,
  "hero_banner": {
    "score": <float 0-10>,
    "observation": "<does headline/hero address the configured narrative and the target persona's #1 concern?>",
    "suggestion": "<specific rewrite>"
  },
  "carousel_flow": {
    "score": <float 0-10>,
    "observation": "<do slides follow the narrative arc: hook → problem → solution → proof? quote what's present/missing>",
    "suggestion": "<specific slide-level fix>"
  },
  "banner_alignment": {
    "score": <float 0-10>,
    "observation": "<does banner copy reinforce the configured narrative or feel generic?>",
    "suggestion": "<specific fix>"
  },
  "page_narrative_arc": {
    "score": <float 0-10>,
    "observation": "<does the full page tell a coherent story aligned to the narrative?>",
    "suggestion": "<specific fix>"
  },
  "cta_language": {
    "score": <float 0-10>,
    "observation": "<does the CTA match the emotional state the narrative should leave the persona in?>",
    "suggestion": "<specific rewrite>"
  },
  "flagged_issues": ["<critical gap 1>", "<critical gap 2>"],
  "persona_matrix": [
    {
      "persona": "<persona name>",
      "doing_right": ["<specific copy or element that works for this persona>", ...],
      "missing": ["<what's absent that this persona needs based on the narrative>", ...]
    }
  ]
}"""


def score_persona_narrative(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = "",
    configured_persona: str = ""
) -> PersonaNarrativeScore:
    log.info(f"Scoring persona × narrative for {pdp.url}")

    # ── Resolve URL-level context — never share across URLs ───────────────────
    url_ctx = _resolve_url_context(
        pdp.url, configured_narrative, configured_persona, context
    )
    persona_name      = url_ctx["persona_name"]
    pain_points       = url_ctx["pain_points"]
    persona_motivations   = url_ctx["persona_motivations"]
    persona_objections    = url_ctx["persona_objections"]
    persona_language_cues = url_ctx["persona_language_cues"]
    narrative_label   = url_ctx["narrative_label"]
    narrative_pillars = url_ctx["narrative_pillars"]

    # All product personas for the persona matrix
    product_cfg = _get_product_cfg(pdp.url)
    all_personas = product_cfg.get("personas", [persona_name]) if product_cfg else [persona_name]

    carousel_summary = ""
    if pdp.carousels:
        slides = "\n".join([f"  Slide {s.index}: {s.copy[:200]}" for s in pdp.carousels])
        carousel_summary = f"\nCAROUSEL SLIDES:\n{slides}"

    banner_summary = ""
    if pdp.banners:
        banners = "\n".join([f"  Banner ({b.location}): {b.copy[:200]}" for b in pdp.banners])
        banner_summary = f"\nBANNERS:\n{banners}"

    persona_desc = "\n".join([f"  - {p}" for p in all_personas])

    # ── Build URL-specific prompt ─────────────────────────────────────────────
    # Only the assigned narrative and persona context are injected here.
    # Other URLs' narratives/personas are deliberately excluded.
    prompt = f"""URL BEING SCORED: {pdp.url}

━━ SCORING CONTEXT (specific to THIS URL only) ━━
ASSIGNED NARRATIVE: {narrative_label}
ASSIGNED PERSONA:   {persona_name}

NARRATIVE PILLARS FOR "{narrative_label}":
{chr(10).join(f"  • {p}" for p in narrative_pillars)}

PERSONA PAIN POINTS FOR "{persona_name}":
{chr(10).join(f"  • {p}" for p in pain_points[:5])}

PERSONA MOTIVATIONS: {', '.join(persona_motivations[:4])}
PERSONA OBJECTIONS:  {', '.join(persona_objections[:4])}
PERSONA LANGUAGE CUES: {', '.join(persona_language_cues[:6])}

BRAND VOICE:
  Dos:    {', '.join(context.brand_voice.dos[:5])}
  Don'ts: {', '.join(context.brand_voice.donts[:5])}

NARRATIVE EMOTIONAL ARC: {context.narrative.emotional_arc}

━━ PDP CONTENT ━━
HEADLINE: {pdp.headline or 'NOT FOUND'}
SUBHEADS: {' | '.join(pdp.subheads[:12]) or 'NONE'}
BODY COPY (first 2500 chars): {' '.join(pdp.body_copy)[:2500]}
CTAs: {' | '.join(pdp.cta_texts) or 'NONE'}
{carousel_summary}
{banner_summary}

━━ INSTRUCTIONS ━━
Score this PDP ONLY against the "{narrative_label}" narrative and the "{persona_name}" persona above.
Do NOT apply other narratives or personas — this URL has one assigned context.

ALL PRODUCT PERSONAS for persona_matrix (generate a row for each):
{persona_desc}

Be specific — quote exact copy lines that work or fail for this narrative."""

    data = call_claude(client, SYSTEM, prompt)

    # Parse persona matrix
    raw_matrix = data.get("persona_matrix", [])
    persona_matrix = [
        PersonaMatrixRow(
            persona=row.get("persona", ""),
            doing_right=row.get("doing_right", []),
            missing=row.get("missing", [])
        )
        for row in raw_matrix if row.get("persona")
    ]

    log.info(
        f"P×N scored: {pdp.url} | "
        f"persona={persona_name} | narrative={narrative_label} | "
        f"overall={data.get('overall', '?')}"
    )

    return PersonaNarrativeScore(
        overall=data["overall"],
        configured_narrative=configured_narrative,
        configured_persona=configured_persona,
        hero_banner=SubScore(name="Hero / Banner", **data["hero_banner"]),
        carousel_flow=SubScore(name="Carousel Flow", **data["carousel_flow"]),
        banner_alignment=SubScore(name="Banner Alignment", **data["banner_alignment"]),
        page_narrative_arc=SubScore(name="Page Narrative Arc", **data["page_narrative_arc"]),
        cta_language=SubScore(name="CTA Language", **data["cta_language"]),
        flagged_issues=data.get("flagged_issues", []),
        persona_matrix=persona_matrix,
        # Audit trail — what was actually used for this URL
        persona_used=persona_name,
        narrative_used=narrative_label,
        pain_points_checked=pain_points[:5],
    )


def _get_product_cfg(url: str) -> dict:
    """Find which product config this URL belongs to."""
    for product in config.get("products", []):
        for u in product.get("urls", []):
            u_str = u["url"] if isinstance(u, dict) else u
            if u_str == url:
                return product
    return {}
