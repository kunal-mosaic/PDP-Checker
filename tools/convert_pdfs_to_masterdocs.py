#!/usr/bin/env python3
"""
Convert existing product PDFs to MasterDoc .md files.
Usage: python tools/convert_pdfs_to_masterdocs.py

Reads persona/product_brief/narrative PDFs for each product,
uses Claude to reformat into clean prose sections, and adds the
Required Claims section (GM compliance + any email-specific items).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingester.pdf_reader import extract_text
import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

client = anthropic.Anthropic()

SECTION_PROMPTS = {
    "persona": (
        "Extract the target customer persona from this document. "
        "Write clean prose covering: who they are, age range, key pain points, "
        "motivations, and how they speak about their problem. 200-300 words max."
    ),
    "product_brief": (
        "Extract the product brief from this document. "
        "Write clean prose covering: what the product is, key ingredients and their roles, "
        "primary benefits, proof points, and differentiators. 200-300 words max."
    ),
    "narrative": (
        "Extract the narrative/messaging pillars from this document. "
        "Write clean prose covering: the core story arc, key narrative pillars, "
        "emotional journey from problem to solution, and key claims. 200-300 words max."
    ),
}


def extract_section(raw_text: str, section_type: str) -> str:
    prompt = SECTION_PROMPTS.get(section_type, "Summarize this document in 200-300 words.")
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": f"{prompt}\n\nDOCUMENT:\n{raw_text[:8000]}"}],
    )
    return response.content[0].text.strip()


# ── Per-product config ─────────────────────────────────────────────────────────

PRODUCTS = [
    {
        "name": "Shilajit Gummies",
        "slug": "shilajit_gummies",
        "pdfs": {
            "persona":       "inputs/pdfs/shilajit_gummies/persona.pdf",
            "product_brief": "inputs/pdfs/shilajit_gummies/product_brief.docx",
            "narrative":     "inputs/pdfs/shilajit_gummies/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance — all nutra products)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "No Added Sugar" must NOT appear (unless product genuinely has no added sugar — verify against label)
- "Healthy" must NOT be used as a direct product descriptor (e.g. "healthy supplement", "healthy formula")

### Ingredients to remove (per Aditya email Jun 2026 — present on PDP but absent on latest label)
- Red Ginseng must NOT be listed in ingredients

### Ingredients to add (per email — present on label but absent on PDP)
- Maize Starch must be listed in ingredients
- Guar Gum must be listed in ingredients

### Manufacturer
- Verify "Manufactured by" text matches latest approved label

### Additional checks (per email)
- Review and update FAQs as required
- Verify and correct the Fulvic Acid claim mentioned within the FAQ section
- NI table must be present on PDP
""",
    },
    {
        "name": "Hair Regrowth Kit S2",
        "slug": "hair_regrowth_s2",
        "pdfs": {
            "persona":       "inputs/pdfs/hair_regrowth_s2/persona.pdf",
            "product_brief": "inputs/pdfs/hair_regrowth_s2/product_brief.pdf",
            "narrative":     "inputs/pdfs/hair_regrowth_s2/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "Healthy" must NOT be used as a direct product descriptor
""",
    },
    {
        "name": "Hair Regrowth Kit S3",
        "slug": "hair_regrowth_s3",
        "pdfs": {
            "persona":       "inputs/pdfs/hair_regrowth_s3/persona.pdf",
            "product_brief": "inputs/pdfs/hair_regrowth_s3/product_brief.pdf",
            "narrative":     "inputs/pdfs/hair_regrowth_s3/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "Healthy" must NOT be used as a direct product descriptor
""",
    },
    {
        "name": "Advanced Hair Regrowth Regime",
        "slug": "advanced_hair_regrowth",
        "pdfs": {
            "persona":       "inputs/pdfs/advanced_hair_regrowth/persona.pdf",
            "product_brief": "inputs/pdfs/advanced_hair_regrowth/product_brief.pdf",
            "narrative":     "inputs/pdfs/advanced_hair_regrowth/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "Healthy" must NOT be used as a direct product descriptor
""",
    },
    {
        "name": "Anti-Hairfall Kit S1",
        "slug": "anti_hairfall_s1",
        "pdfs": {
            "persona":       "inputs/pdfs/anti_hairfall_s1/persona.pdf",
            "product_brief": "inputs/pdfs/anti_hairfall_s1/product_brief.pdf",
            "narrative":     "inputs/pdfs/anti_hairfall_s1/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "Healthy" must NOT be used as a direct product descriptor
""",
    },
    {
        "name": "Hair Gummies",
        "slug": "hair_gummies",
        "pdfs": {
            "persona":       "inputs/pdfs/hair_gummies_amj/persona.pdf",
            "product_brief": "inputs/pdfs/hair_gummies_amj/product_brief.pdf",
            "narrative":     "inputs/pdfs/hair_gummies_amj/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance — all nutra products)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "No Added Sugar" must NOT appear (unless product genuinely has no added sugar — verify against label)
- "Healthy" must NOT be used as a direct product descriptor

### Ingredients to remove (per Aditya email Jun 2026 — present on PDP but absent on latest label)
- Iron must NOT be listed in ingredients

### Manufacturer (per email)
- Manufactured By must be updated to: Advama (verify exact text matches latest label)

### Additional checks (per email)
- NI table must be present on PDP based on the latest label
- Verify all ingredients on PDP are fully aligned with latest label
- Review and validate ingredient information across all PDP sections for consistency
""",
    },
    {
        "name": "Beard Growth Kit",
        "slug": "beard_growth_kit",
        "pdfs": {
            "persona":       "inputs/pdfs/beard/persona.pdf",
            "product_brief": "inputs/pdfs/beard/product_brief.docx",
            "narrative":     "inputs/pdfs/beard/narrative.pdf",
        },
        "required_claims": """\
### Must NOT appear on PDP (GM Compliance)
- "100% Natural" must NOT appear anywhere on PDP (text or images)
- "No Side Effects" must NOT appear
- "Healthy" must NOT be used as a direct product descriptor

### Manufacturer (per Aditya email Jun 2026)
- Verify "Manufactured by" text matches latest approved label/artwork

### Additional checks (per email)
- Update the entire formulation as per the latest artwork
- NI table must be present on PDP
- Verify ingredient list is fully aligned with latest artwork
""",
    },
]


def convert_product(product: dict) -> str:
    print(f"\n{'='*50}")
    print(f"  Converting: {product['name']}")
    print(f"{'='*50}")

    sections = {}
    for section_key, pdf_path in product["pdfs"].items():
        try:
            raw = extract_text(pdf_path)
            print(f"  [{section_key}] extracted {len(raw):,} chars from {Path(pdf_path).name}")
            sections[section_key] = extract_section(raw, section_key)
            print(f"  [{section_key}] formatted OK")
        except Exception as e:
            print(f"  [{section_key}] WARNING: {e}")
            sections[section_key] = f"[Could not extract — check {pdf_path}]"

    md = (
        f"## Persona\n{sections.get('persona', '[TBD]')}\n\n"
        f"## Product Brief\n{sections.get('product_brief', '[TBD]')}\n\n"
        f"## Narrative\n{sections.get('narrative', '[TBD]')}\n\n"
        f"## Required Claims\n{product['required_claims']}\n"
    )
    return md


def main():
    out_dir = Path("inputs/masterdocs")
    out_dir.mkdir(parents=True, exist_ok=True)

    converted = []
    skipped = []

    for product in PRODUCTS:
        out_path = out_dir / f"{product['slug']}.md"
        if out_path.exists():
            print(f"SKIP {out_path.name} — already exists (delete to regenerate)")
            skipped.append(out_path.name)
            continue

        md = convert_product(product)
        out_path.write_text(md, encoding="utf-8")
        print(f"\nSAVED: {out_path}")
        converted.append(out_path.name)

    print(f"\n{'='*50}")
    print(f"Done. Converted: {len(converted)} | Skipped: {len(skipped)}")
    if converted:
        print("Created:")
        for f in converted:
            print(f"  inputs/masterdocs/{f}")
    print("\nNext: review the .md files, then run tools/wire_masterdocs_to_config.py")


if __name__ == "__main__":
    main()
