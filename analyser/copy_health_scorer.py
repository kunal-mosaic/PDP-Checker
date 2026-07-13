"""
Copy Health Scorer — three distinct checks mapped to the Hygiene Check tabs:
  1. Claims Alignment  — flags PDP claims vs product brief (numbers, absolutes)
  2. Brand Guidelines  — flags tone violations and banned words
  3. Spell / Grammar   — flags errors, punctuation, unclear copy

GM/compliance claims (100% Natural, No Side Effects, Vegan, etc.) are no longer scanned
here — they're driven entirely by each product's MasterDoc `## Required Claims` section
and checked in analyser/packaging_scorer.py::_check_required_claims(), then merged into
this scorer's claims_flags by main.py after the packaging check runs. This keeps GM
compliance as a single masterdoc-driven source instead of a second, hardcoded phrase list.
"""

import re
import anthropic
from analyser.models import CopyHealthScore, SubScore, ClaimFlag, TextInsight
from analyser.claude_client import call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from utils.logger import get_logger

log = get_logger("copy_health_scorer")

# ── Claims Alignment prompt ────────────────────────────────────────────────────

CLAIMS_SYSTEM = """You are a regulatory and accuracy reviewer for a health/wellness brand.
You will be given PDP copy and the approved product brief.
Your job: identify every claim on the PDP that goes beyond what the brief supports.
Flag anything not explicitly backed: unverified numbers, superlatives, absolute words
(100%, best, most, proven, guaranteed), ingredient claims not listed in the brief.
Also confirm claims that DO match.
Return ONLY valid JSON — no markdown.

Schema:
{
  "overall": <float 0-10>,
  "claims_alignment": {
    "score": <float 0-10>,
    "observation": "<summary: how many claims checked, key issues>",
    "suggestion": "<top priority fix>"
  },
  "claims": [
    {
      "text": "<exact claim text>",
      "status": "ok" | "flagged" | "warning",
      "reason": "<why ok/flagged — quote from brief if ok, explain gap if flagged>"
    }
  ]
}

Statuses:
  ok      — claim is directly supported by product brief
  warning — claim is partially supported or implied, but not explicit
  flagged — claim has no backing in brief, contains unsupported numbers, or uses
             absolute language not present in the brief
"""


# ── Brand Guidelines prompt ────────────────────────────────────────────────────

BRAND_SYSTEM = """You are a brand voice and compliance reviewer.
You will be given PDP copy, the brand voice guidelines, and a list of banned words/phrases.
Flag every violation: wrong tone, banned words used, missing required language, or copy
that contradicts the brand's positioning.
Return ONLY valid JSON — no markdown.

Schema:
{
  "overall": <float 0-10>,
  "brand_guidelines": {
    "score": <float 0-10>,
    "observation": "<what tone issues / violations were found>",
    "suggestion": "<specific rewrite recommendation>"
  },
  "brand_flags": [
    "<exact violation — quote the problematic text and explain the rule broken>"
  ]
}"""


# ── Spell / Grammar prompt ─────────────────────────────────────────────────────

SPELL_SYSTEM = """You are a meticulous copy editor. Check the PDP copy for:
- Spelling errors
- Grammar mistakes
- Punctuation problems
- Sentence clarity issues
- Inconsistent capitalisation
List every error with the exact incorrect text and the correction.
Return ONLY valid JSON — no markdown.

Schema:
{
  "overall": <float 0-10>,
  "spell_grammar": {
    "score": <float 0-10>,
    "observation": "<list every error found with exact text>",
    "suggestion": "<corrected versions>"
  },
  "flagged_errors": [
    "<exact incorrect text> → <correction>"
  ]
}"""


TEXT_INSIGHT_SYSTEM = """You are a content strategist auditing a PDP copy against its configured narrative and brand guidelines.
Analyse the copy across 5 dimensions and return structured insights.
Return ONLY valid JSON — no markdown.

Schema:
{
  "insights": [
    {
      "category": "<one of: Forbidden Word | Missing Hook | Narrative Gap | Weak CTA | Missing Social Proof | Ingredient Gap | Tone Mismatch | Missing Proof Point>",
      "severity": "<critical | warning | ok>",
      "finding": "<short headline — max 12 words>",
      "detail": "<specific evidence from the copy — quote exact text where possible>"
    }
  ]
}

Categories:
  Forbidden Word      — any word from the NEVER MENTION list is present on the PDP
  Missing Hook        — none of the approved narrative hooks appear in the headline/subheads
  Narrative Gap       — a key narrative element (JTBD, mandate) is entirely absent
  Weak CTA            — CTA is generic, doesn't match persona's emotional state
  Missing Social Proof — no numbers / % / testimonial language present
  Ingredient Gap      — a key ingredient mentioned in the brief is absent from copy
  Tone Mismatch       — copy uses language inconsistent with brand voice
  Missing Proof Point — approved proof points from brief not reflected in copy
"""


def score_copy_health(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = ""
) -> CopyHealthScore:
    log.info(f"Scoring copy health for {pdp.url}")

    all_copy = _build_copy_block(pdp)
    brief_claims = _build_brief_claims(context)

    # ── 1. Claims check ────────────────────────────────────────────────────────
    claims_prompt = f"""PRODUCT BRIEF CLAIMS (approved):
{brief_claims}

PDP COPY TO CHECK:
{all_copy}"""

    claims_data = call_claude(client, CLAIMS_SYSTEM, claims_prompt)

    raw_claims = claims_data.get("claims", [])
    claim_flags = [
        ClaimFlag(
            text=c.get("text", ""),
            status=c.get("status", "warning"),
            reason=c.get("reason", "")
        )
        for c in raw_claims if c.get("text")
    ]

    # ── 2. Brand guidelines check ──────────────────────────────────────────────
    brand_prompt = f"""BRAND VOICE GUIDELINES:
Tone: {', '.join(context.brand_voice.tone_descriptors)}
Do: {'; '.join(context.brand_voice.dos[:5])}
Don't: {'; '.join(context.brand_voice.donts[:5])}
Banned words: {', '.join(context.brand_voice.banned_words)}
Power words (should use): {', '.join(context.brand_voice.power_words[:10])}

PDP COPY TO CHECK:
{all_copy}"""

    brand_data = call_claude(client, BRAND_SYSTEM, brand_prompt)

    # ── 3. Spell / grammar check ───────────────────────────────────────────────
    spell_prompt = f"""PDP COPY TO PROOFREAD:
META TITLE: {pdp.meta_title or 'NOT FOUND'}
META DESCRIPTION: {pdp.meta_description or 'NOT FOUND'}
{all_copy}"""

    spell_data = call_claude(client, SPELL_SYSTEM, spell_prompt)

    # ── 4. Text layer insights ──────────────────────────────────────────────────
    never_mention = ["testosterone", "libido", "body shaming", "pre-workout",
                     "stamina", "performance", "himalayan", "ancient indian medicine"]
    insight_prompt = f"""CONFIGURED NARRATIVE: {configured_narrative or 'Not specified'}

NEVER MENTION (forbidden topics/words for this product): {', '.join(never_mention)}

KEY INGREDIENTS FROM BRIEF: {', '.join(context.product_brief.key_ingredients[:10])}
APPROVED PROOF POINTS:
{chr(10).join(f'  - {p}' for p in context.product_brief.proof_points[:6])}

BRAND TONE: {', '.join(context.brand_voice.tone_descriptors)}
BRAND DON'TS: {', '.join(context.brand_voice.donts[:5])}
BANNED WORDS: {', '.join(context.brand_voice.banned_words[:10])}

PDP HEADLINE: {pdp.headline or 'NOT FOUND'}
PDP SUBHEADS: {' | '.join(pdp.subheads[:10]) or 'NONE'}
PDP CTAs: {' | '.join(pdp.cta_texts) or 'NONE'}
{all_copy[:3000]}

Produce 6-10 insights. Quote exact copy. Be specific about what's present or absent."""

    insight_data = call_claude(client, TEXT_INSIGHT_SYSTEM, insight_prompt)
    text_insights = [
        TextInsight(
            category=i.get("category", ""),
            severity=i.get("severity", "warning"),
            finding=i.get("finding", ""),
            detail=i.get("detail", "")
        )
        for i in insight_data.get("insights", [])
        if i.get("finding")
    ]

    # ── Combine into CopyHealthScore ───────────────────────────────────────────
    c_score = claims_data.get("claims_alignment", {}).get("score", 7.0)
    b_score = brand_data.get("brand_guidelines", {}).get("score", 7.0)
    s_score = spell_data.get("spell_grammar", {}).get("score", 7.0)
    overall = round(c_score * 0.4 + b_score * 0.3 + s_score * 0.3, 1)

    return CopyHealthScore(
        overall=overall,
        claims_alignment=SubScore(
            name="Claims Alignment",
            **claims_data.get("claims_alignment", {
                "score": 7.0, "observation": "No data", "suggestion": ""
            })
        ),
        brand_guidelines=SubScore(
            name="Brand Guidelines",
            **brand_data.get("brand_guidelines", {
                "score": 7.0, "observation": "No data", "suggestion": ""
            })
        ),
        spell_grammar=SubScore(
            name="Spell / Grammar",
            **spell_data.get("spell_grammar", {
                "score": 7.0, "observation": "No data", "suggestion": ""
            })
        ),
        claims_flags=claim_flags,
        brand_flags=brand_data.get("brand_flags", []),
        flagged_errors=spell_data.get("flagged_errors", []),
        text_insights=text_insights,
    )


def _build_copy_block(pdp: PDPTextData) -> str:
    parts = []
    if pdp.headline:
        parts.append(f"HEADLINE: {pdp.headline}")
    if pdp.subheads:
        parts.append("SUBHEADS:\n" + "\n".join(f"  - {s}" for s in pdp.subheads[:20]))
    if pdp.body_copy:
        parts.append("BODY COPY:\n" + "\n".join(f"  - {b}" for b in pdp.body_copy[:40]))
    if pdp.cta_texts:
        parts.append(f"CTAs: {' | '.join(pdp.cta_texts)}")
    return "\n\n".join(parts)


def _build_hooks_hint(context: IngestedContext) -> str:
    """Return a short list of approved hooks from narrative key_claims."""
    hooks = getattr(context.narrative, 'key_claims', [])
    if not hooks:
        return "Not available"
    return "\n".join(f"  - {h}" for h in hooks[:6])


def _build_brief_claims(context: IngestedContext) -> str:
    brief = context.product_brief
    narrative = context.narrative
    lines = []
    if brief.primary_benefits:
        lines.append("Primary benefits: " + ", ".join(brief.primary_benefits))
    if brief.key_ingredients:
        lines.append("Key ingredients: " + ", ".join(brief.key_ingredients))
    if brief.proof_points:
        lines.append("Approved proof points:\n" + "\n".join(f"  - {p}" for p in brief.proof_points))
    if brief.differentiators:
        lines.append("Differentiators:\n" + "\n".join(f"  - {d}" for d in brief.differentiators))
    if narrative.key_claims:
        lines.append("Approved claims from narrative:\n" + "\n".join(f"  - {c}" for c in narrative.key_claims))
    return "\n".join(lines) if lines else "No product brief available."
