from pydantic import BaseModel
from typing import List, Optional


class NITableRow(BaseModel):
    ingredient: str
    value: str  # e.g. "500 mg", "10 mcg"


class RequiredClaimsContext(BaseModel):
    """Compliance rules sourced from the product MasterDoc."""
    health_claims_required: List[str] = []   # must appear on PDP
    prohibited_claims: List[str] = []         # must NOT appear on PDP
    ni_table: List[NITableRow] = []           # expected NI table values
    manufacturer: str = ""                    # required manufacturer text
    additional_checks: List[str] = []         # any other requirements


class PersonaProfile(BaseModel):
    name: str                          # e.g. "The Stressed Professional"
    age_range: str                     # e.g. "28-40"
    description: str                   # who they are in 2-3 sentences
    top_concerns: List[str]            # top 3-5 pain points (used for scoring)
    motivations: List[str]             # what they want to achieve
    language_cues: List[str]           # words/phrases they use or respond to
    objections: List[str]              # what stops them from buying


class NarrativePillars(BaseModel):
    core_story: str                    # the one-line story arc
    pillars: List[str]                 # 3-5 key narrative pillars
    emotional_arc: str                 # problem → empathy → solution → proof
    key_claims: List[str]              # product claims to validate on PDP


class BrandVoice(BaseModel):
    tone_descriptors: List[str]        # e.g. ["warm", "confident", "no-fluff"]
    dos: List[str]                     # things to do in copy
    donts: List[str]                   # things to avoid
    power_words: List[str]             # words the brand owns
    banned_words: List[str]            # words never to use


class ProductBrief(BaseModel):
    product_name: str
    tagline: str
    key_ingredients: List[str]
    primary_benefits: List[str]        # top benefits to highlight
    target_persona_concerns: List[str] # concerns this product addresses
    proof_points: List[str]            # clinical studies, certifications etc.
    differentiators: List[str]         # what makes it different


class IngestedContext(BaseModel):
    """Full context extracted from all PDFs or MasterDoc — passed to every analysis step"""
    persona: PersonaProfile
    narrative: NarrativePillars
    brand_voice: BrandVoice
    product_brief: ProductBrief
    product_name: str
    required_claims: Optional[RequiredClaimsContext] = None  # from MasterDoc ## Required Claims section
