import json
import hashlib
import anthropic
from pathlib import Path
from typing import Optional
from ingester.models import (
    PersonaProfile, NarrativePillars,
    BrandVoice, ProductBrief, IngestedContext,
    RequiredClaimsContext, NITableRow,
)
from ingester.pdf_reader import load_all_pdfs
from utils.config_loader import get_env
from utils.logger import get_logger

log = get_logger("extractor")

# Ingested context is cached in outputs/ingest_cache/{product_slug}.json
# Cache is invalidated automatically when any PDF file changes (md5 fingerprint).
_INGEST_CACHE_DIR = Path("outputs/ingest_cache")


def _product_slug(name: str) -> str:
    import re
    return re.sub(r"[^\w]", "_", name.lower()).strip("_")


def _pdf_fingerprint(pdf_paths: dict) -> str:
    """MD5 of the combined contents of all PDFs — changes when any file is updated."""
    h = hashlib.md5()
    for key in sorted(pdf_paths.keys()):
        path = Path(pdf_paths[key])
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()


def _load_ingest_cache(product_name: str, fingerprint: str) -> Optional[IngestedContext]:
    """Return cached IngestedContext if it exists and fingerprint matches."""
    cache_file = _INGEST_CACHE_DIR / f"{_product_slug(product_name)}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        if data.get("fingerprint") != fingerprint:
            log.info(f"Ingest cache invalid for '{product_name}' — PDFs changed, re-ingesting")
            return None
        log.info(f"Ingest cache hit for '{product_name}' — skipping Claude extraction")
        return IngestedContext(**data["context"])
    except Exception as e:
        log.warning(f"Ingest cache read failed: {e} — re-ingesting")
        return None


def _save_ingest_cache(product_name: str, fingerprint: str, context: IngestedContext):
    _INGEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _INGEST_CACHE_DIR / f"{_product_slug(product_name)}.json"
    payload = {
        "fingerprint": fingerprint,
        "product_name": product_name,
        "context": context.model_dump(),
    }
    cache_file.write_text(json.dumps(payload, indent=2))
    log.info(f"Ingest cache saved for '{product_name}'  →  {cache_file.name}")


def _call_claude(client: anthropic.Anthropic, system: str, user: str) -> dict:
    """Call Claude and parse JSON response."""
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    text = response.content[0].text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    return json.loads(text)


def extract_persona(client: anthropic.Anthropic, raw_text: str) -> PersonaProfile:
    log.info("Extracting persona profile...")
    system = (
        "You are a brand strategist. Extract structured persona data from the document. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "name": "persona name or label",\n'
        '  "age_range": "e.g. 28-40",\n'
        '  "description": "2-3 sentence summary of who they are",\n'
        '  "top_concerns": ["concern 1", "concern 2", "concern 3"],\n'
        '  "motivations": ["motivation 1", "motivation 2"],\n'
        '  "language_cues": ["phrase or word they use", "..."],\n'
        '  "objections": ["objection 1", "objection 2"]\n'
        "}"
    )
    data = _call_claude(client, system, f"PERSONA DOCUMENT:\n\n{raw_text}")
    profile = PersonaProfile(**data)
    log.info(f"Persona extracted → {profile.name} | Concerns: {len(profile.top_concerns)}")
    return profile


def extract_narrative(client: anthropic.Anthropic, raw_text: str) -> NarrativePillars:
    # Return a default if narrative doc not provided
    if raw_text.startswith("[") and "not provided" in raw_text:
        log.warning("Narrative PDF not found — using placeholder. Add inputs/pdfs/narrative.pdf for better scoring.")
        return NarrativePillars(
            core_story="Narrative document not provided",
            pillars=["Add narrative.pdf to inputs/pdfs/ for full analysis"],
            emotional_arc="problem → solution → proof",
            key_claims=[]
        )

    log.info("Extracting narrative pillars...")
    system = (
        "You are a brand storyteller. Extract the narrative structure from this document. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "core_story": "one-line story arc",\n'
        '  "pillars": ["pillar 1", "pillar 2", "pillar 3"],\n'
        '  "emotional_arc": "problem → empathy → solution → proof (customised to this brand)",\n'
        '  "key_claims": ["claim 1", "claim 2", "claim 3"]\n'
        "}"
    )
    data = _call_claude(client, system, f"NARRATIVE DOCUMENT:\n\n{raw_text}")
    narrative = NarrativePillars(**data)
    log.info(f"Narrative extracted → {len(narrative.pillars)} pillars, {len(narrative.key_claims)} claims")
    return narrative


def extract_brand_voice(client: anthropic.Anthropic, raw_text: str) -> BrandVoice:
    log.info("Extracting brand voice guidelines...")
    system = (
        "You are a copy director. Extract brand voice rules from this guidelines document. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "tone_descriptors": ["e.g. warm", "confident", "no-fluff"],\n'
        '  "dos": ["do this in copy", "..."],\n'
        '  "donts": ["never do this", "..."],\n'
        '  "power_words": ["words the brand owns", "..."],\n'
        '  "banned_words": ["words never to use", "..."]\n'
        "}"
    )
    data = _call_claude(client, system, f"BRAND GUIDELINES DOCUMENT:\n\n{raw_text}")
    voice = BrandVoice(**data)
    log.info(f"Brand voice extracted → tone: {voice.tone_descriptors}")
    return voice


def extract_product_brief(client: anthropic.Anthropic, raw_text: str) -> ProductBrief:
    log.info("Extracting product brief...")
    system = (
        "You are a product marketer. Extract structured product information from this brief. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "product_name": "full product name",\n'
        '  "tagline": "the product tagline",\n'
        '  "key_ingredients": ["ingredient 1", "..."],\n'
        '  "primary_benefits": ["benefit 1", "benefit 2"],\n'
        '  "target_persona_concerns": ["concern this product addresses", "..."],\n'
        '  "proof_points": ["clinical study / certification / award", "..."],\n'
        '  "differentiators": ["what makes it different", "..."]\n'
        "}"
    )
    data = _call_claude(client, system, f"PRODUCT BRIEF DOCUMENT:\n\n{raw_text}")
    brief = ProductBrief(**data)
    log.info(f"Product brief extracted → {brief.product_name}")
    return brief


def extract_required_claims(client: anthropic.Anthropic, section_text: str) -> RequiredClaimsContext:
    """Extract required compliance claims from the ## Required Claims section of a MasterDoc."""
    if not section_text or not section_text.strip():
        return RequiredClaimsContext()
    log.info("Extracting required claims from MasterDoc...")
    system = (
        "You are a compliance analyst. Extract required claims from this product MasterDoc section. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "health_claims_required": ["No Added Sugar", "No Side Effects", ...],\n'
        '  "prohibited_claims": ["100% Natural", ...],\n'
        '  "ni_table": [{"ingredient": "Shilajit", "value": "500 mg"}, ...],\n'
        '  "manufacturer": "exact manufacturer text required on label",\n'
        '  "additional_checks": ["any other requirement to verify on PDP", ...]\n'
        "}\n"
        "If a field has no data, use an empty list or empty string."
    )
    data = _call_claude(client, system, f"REQUIRED CLAIMS SECTION:\n\n{section_text}")
    ni_rows = [NITableRow(**r) for r in data.get("ni_table", [])]
    return RequiredClaimsContext(
        health_claims_required=data.get("health_claims_required", []),
        prohibited_claims=data.get("prohibited_claims", []),
        ni_table=ni_rows,
        manufacturer=data.get("manufacturer", ""),
        additional_checks=data.get("additional_checks", []),
    )


def _parse_masterdoc_sections(content: str) -> dict:
    """Parse a MasterDoc .md file into a dict of {section_name: body_text}."""
    sections = {}
    current_heading = None
    current_lines = []
    for line in content.split("\n"):
        if line.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()
    return sections


def ingest_masterdoc(product_config: dict) -> IngestedContext:
    """
    Ingest product context from a MasterDoc .md file.
    Called when product_config has a 'masterDoc' key.
    """
    client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))
    product_name = product_config["name"]
    md_path = Path(product_config["masterDoc"])

    if not md_path.exists():
        raise FileNotFoundError(f"MasterDoc not found: {md_path}")

    log.info(f"Reading MasterDoc: {md_path}")

    # Cache check — skip Claude if the .md file hasn't changed
    fingerprint = hashlib.md5(md_path.read_bytes()).hexdigest()
    cached = _load_ingest_cache(product_name, fingerprint)
    if cached:
        return cached

    content = md_path.read_text(encoding="utf-8")
    sections = _parse_masterdoc_sections(content)

    persona_text   = sections.get("Persona", "")
    brief_text     = sections.get("Product Brief", "")
    narrative_text = sections.get("Narrative", "")
    claims_text    = sections.get("Required Claims", "")

    # Use placeholder brand voice (MasterDoc doesn't carry brand guidelines — shared PDF still used)
    brand_guidelines_path = product_config.get("pdfs", {}).get("brand_guidelines", "")
    if brand_guidelines_path and Path(brand_guidelines_path).exists():
        from ingester.pdf_reader import load_all_pdfs
        brand_raw = load_all_pdfs({"brand_guidelines": brand_guidelines_path})
        voice = extract_brand_voice(client, brand_raw["brand_guidelines"])
    else:
        voice = BrandVoice(
            tone_descriptors=["clean", "science-backed", "direct"],
            dos=["Use evidence-based language"],
            donts=["Avoid exaggerated claims"],
            power_words=[],
            banned_words=[],
        )

    def _is_tbd(text: str) -> bool:
        t = text.strip()
        return not t or t.startswith("[TBD") or t.startswith("# TBD") or t.lower().startswith("tbd")

    persona   = _placeholder_persona(product_name) if _is_tbd(persona_text) else extract_persona(client, persona_text)
    narrative = _placeholder_narrative() if _is_tbd(narrative_text) else extract_narrative(client, narrative_text)
    brief     = _placeholder_brief(product_name) if _is_tbd(brief_text) else extract_product_brief(client, brief_text)
    req_claims = None if _is_tbd(claims_text) else extract_required_claims(client, claims_text)

    context = IngestedContext(
        persona=persona,
        narrative=narrative,
        brand_voice=voice,
        product_brief=brief,
        product_name=product_name,
        required_claims=req_claims,
    )

    _save_ingest_cache(product_name, fingerprint, context)
    log.info(f"MasterDoc ingestion complete for: {product_name}")
    return context


def _placeholder_persona(product_name: str) -> PersonaProfile:
    return PersonaProfile(
        name="[TBD — add ## Persona section to MasterDoc]",
        age_range="",
        description=f"No persona defined yet for {product_name}.",
        top_concerns=[],
        motivations=[],
        language_cues=[],
        objections=[],
    )


def _placeholder_narrative() -> NarrativePillars:
    return NarrativePillars(
        core_story="[TBD — add ## Narrative section to MasterDoc]",
        pillars=[],
        emotional_arc="",
        key_claims=[],
    )


def _placeholder_brief(product_name: str) -> ProductBrief:
    return ProductBrief(
        product_name=product_name,
        tagline="",
        key_ingredients=[],
        primary_benefits=[],
        target_persona_concerns=[],
        proof_points=[],
        differentiators=[],
    )


def ingest(product_config: dict) -> IngestedContext:
    """
    Main entry point. Pass a product's config block.
    Routes to MasterDoc reader if 'masterDoc' key is present, otherwise reads PDFs.
    Returns a fully populated IngestedContext.
    """
    if product_config.get("masterDoc"):
        return ingest_masterdoc(product_config)

    client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))

    product_name = product_config["name"]
    pdf_paths    = product_config["pdfs"]

    log.info(f"Starting ingestion for: {product_name}")

    # ── Cache check — skip Claude if PDFs haven't changed ────────────────────
    fingerprint = _pdf_fingerprint(pdf_paths)
    cached = _load_ingest_cache(product_name, fingerprint)
    if cached:
        return cached

    # ── Full extraction via Claude ────────────────────────────────────────────
    raw_texts = load_all_pdfs(pdf_paths)

    persona   = extract_persona(client, raw_texts["persona"])
    narrative = extract_narrative(client, raw_texts["narrative"])
    voice     = extract_brand_voice(client, raw_texts["brand_guidelines"])
    brief     = extract_product_brief(client, raw_texts["product_brief"])

    context = IngestedContext(
        persona=persona,
        narrative=narrative,
        brand_voice=voice,
        product_brief=brief,
        product_name=product_name,
    )

    _save_ingest_cache(product_name, fingerprint, context)
    log.info(f"Ingestion complete for: {context.product_name}")
    return context
