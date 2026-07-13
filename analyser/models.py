from pydantic import BaseModel
from typing import List, Optional


class SubScore(BaseModel):
    name: str
    score: float          # 0-10
    observation: str      # what was found
    suggestion: str       # what to fix


class SectionFlowIssue(BaseModel):
    section: str          # section heading as it appears on the PDP
    current_position: int # 1-based index in current order
    recommended_position: int
    reason: str           # why it should move


class SectionFlowScore(BaseModel):
    score: float                          # 0-10
    current_order: List[str] = []         # section headings in current page order
    missing_sections: List[str] = []      # sections that should exist but don't
    out_of_order: List[SectionFlowIssue] = []  # sections in wrong position
    redundant_sections: List[str] = []    # sections that duplicate another
    observation: str = ""                 # overall flow assessment
    suggestion: str = ""                  # top priority reorder action


class ReviewsScore(BaseModel):
    overall: float
    freshness: SubScore
    rating_distribution: SubScore
    theme_alignment: SubScore
    negative_handling: SubScore
    flagged_issues: List[str] = []
    # Set to "incomplete_data" when review dates are all missing/unparseable.
    # Downstream: freshness score is unreliable; do not penalise silently.
    score_status: Optional[str] = None      # None | "incomplete_data"
    freshness_warning: Optional[str] = None # human-readable warning when score_status set


class PersonaMatrixRow(BaseModel):
    persona: str              # e.g. "Tejas"
    doing_right: List[str]   # what the PDP does well for this persona
    missing: List[str]        # what's absent / gaps for this persona


class PersonaNarrativeScore(BaseModel):
    overall: float
    configured_narrative: str = ""   # from config.yaml
    configured_persona: str = ""
    hero_banner: SubScore
    carousel_flow: SubScore
    banner_alignment: SubScore
    page_narrative_arc: SubScore
    cta_language: SubScore
    flagged_issues: List[str] = []
    persona_matrix: List[PersonaMatrixRow] = []  # per-persona doing right vs missing
    # Audit trail — what was actually used when scoring this URL
    persona_used: str = ""           # persona name sent to Claude
    narrative_used: str = ""         # narrative label sent to Claude
    pain_points_checked: List[str] = []  # top concerns used in the prompt


class ClaimFlag(BaseModel):
    text: str            # the exact claim found on the PDP
    status: str          # "ok" | "flagged" | "warning"
    reason: str          # why it's flagged (or confirmed)


class TextInsight(BaseModel):
    category: str    # e.g. "Forbidden Word", "Missing Hook", "Narrative Gap"
    severity: str    # "critical" | "warning" | "ok"
    finding: str     # short headline
    detail: str      # explanation


class CopyHealthScore(BaseModel):
    overall: float
    # Sub-scores (map to 3 Hygiene Check sub-tabs)
    spell_grammar: SubScore
    brand_guidelines: SubScore
    claims_alignment: SubScore
    # Structured flag lists (shown in each sub-tab)
    # claims_flags holds BOTH the brief-vs-PDP accuracy check AND, appended by main.py
    # after the packaging step, every MasterDoc "Required Claims" result (GM compliance,
    # prohibited phrases, ingredient/manufacturer checks) — single masterdoc-driven source.
    claims_flags: List[ClaimFlag] = []       # Claims sub-tab
    brand_flags: List[str] = []              # Brand Guidelines sub-tab
    flagged_errors: List[str] = []           # Spell Check sub-tab
    # Text Layer insights
    text_insights: List[TextInsight] = []


class RequiredClaimCheck(BaseModel):
    claim: str                          # the required claim or rule text
    claim_type: str                     # "required" | "prohibited" | "ni_value" | "manufacturer"
    status: str                         # "present" | "absent" | "violated" | "cannot_verify"
    found_text: Optional[str] = None    # exact text found on PDP (if any)
    notes: str = ""


class PackagingSKUResult(BaseModel):
    sku_name: str
    version: str                    # e.g. "V7"
    drive_folder_url: str
    ni_table_present: bool = False
    ni_table_matches: Optional[bool] = None   # None = could not compare
    ni_table_diff: List[str] = []             # ingredients missing or extra
    ni_packaging_values: List[dict] = []      # [{ingredient, value}] from packaging
    ni_pdp_values: List[dict] = []            # [{ingredient, value}] from PDP
    product_photo_present: bool = False
    packaging_match: Optional[bool] = None    # None = no packaging files to compare
    packaging_mismatch_details: List[dict] = []  # [{element, on_packaging, on_pdp}]
    packaging_images_b64: List[str] = []      # base64 JPGs of packaging pages
    pdp_product_images_b64: List[str] = []    # base64 JPGs of PDP product images
    ni_table_pdp_image_b64: Optional[str] = None  # the specific PDP image the NI table was found on


class PackagingScore(BaseModel):
    overall: float = 0.0
    ni_table_check: SubScore = SubScore(name="NI Table", score=0.0, observation="Not run", suggestion="")
    product_photo_check: SubScore = SubScore(name="Product Photo", score=0.0, observation="Not run", suggestion="")
    packaging_match_check: SubScore = SubScore(name="Latest Packaging Match", score=0.0, observation="Not run", suggestion="")
    sku_results: List[PackagingSKUResult] = []
    required_claims_checks: List[RequiredClaimCheck] = []  # from MasterDoc verification
    error: Optional[str] = None


class VisualDesignScore(BaseModel):
    overall: float
    human_presence: SubScore
    proof_prominence: SubScore
    ingredient_imagery: SubScore
    before_after: SubScore
    lifestyle_shots: SubScore
    visual_hierarchy_brand: SubScore
    # Packaging sub-scores (new — added from Version Control Sheet comparison)
    ni_table_present: SubScore = SubScore(name="NI Table Present", score=0.0, observation="Not checked", suggestion="")
    product_photo_present: SubScore = SubScore(name="Product Photo Present", score=0.0, observation="Not checked", suggestion="")
    latest_packaging_match: SubScore = SubScore(name="Latest Packaging Match", score=0.0, observation="Not checked", suggestion="")
    flagged_issues: List[str] = []
    section_flow: Optional[SectionFlowScore] = None  # section order analysis


class AdGap(BaseModel):
    angle: str              # e.g. "Daily Energy"
    conv_rate: str          # e.g. "3.9%" — why it matters
    what_is_missing: str    # specific copy/element not on PDP
    what_to_add: str        # exact suggestion: copy line, placement, format
    where_to_add: str       # e.g. "Carousel slide 2", "Hero headline", "Banner"


class AdAlignmentScore(BaseModel):
    overall: float
    top_converting_angles: List[str] = []   # angles from ads with high Conv. %
    angles_present_on_pdp: List[str] = []   # which ones are on the PDP
    gaps: List[AdGap] = []                  # rich gap objects with suggestions
    atc_drop_off_addressed: SubScore        # is ATC drop-off addressed in copy?
    flagged_gaps: List[str] = []            # kept for legacy/RCA use


class RCAItem(BaseModel):
    culprit_score: str        # e.g. "Visual Design → Proof Prominence"
    score_value: float
    evidence: str             # exact copy/visual/data point
    why_it_matters: str       # impact on persona / conversion
    fix: str                  # specific, actionable recommendation


class PDPAnalysisResult(BaseModel):
    """Complete analysis result for one PDP URL."""
    url: str
    product_name: str
    analysed_at: str

    # Individual scores
    reviews: ReviewsScore
    persona_narrative: PersonaNarrativeScore
    copy_health: CopyHealthScore
    visual_design: VisualDesignScore
    ad_alignment: AdAlignmentScore
    packaging: Optional[PackagingScore] = None   # None if version_control_skus not configured

    # Overall
    overall_score: float
    status: str              # "healthy" | "attention" | "critical"
    rca: List[RCAItem] = []  # populated if overall < 8

    # Regression tracking (populated by RegressionAgent after scoring)
    delta: Optional[float] = None           # current - previous overall (None = first run)
    delta_scores: dict = {}                 # per-dimension deltas {"reviews": -0.3, ...}
    regression_flag: bool = False           # True if overall dropped > 0.5 points
