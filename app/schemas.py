"""
All Pydantic models for the project live here.

Keeping every shape in one file because the project is small enough that
jumping between schema files costs more than scrolling one long file.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums (pinned vocabularies — change these and the whole system reacts)
# ---------------------------------------------------------------------------


class Engine(str, Enum):
    STAGE = "e1_stage"
    IRRIGATION = "e2_irrigation"
    NUTRITION = "e3_nutrition"
    CROP_HEALTH = "e4_crop_health"
    YIELD = "e5_yield"
    FINANCIAL = "e6_financial"


class DocType(str, Enum):
    # ── Standard required types (one active per crop) ────────────────────
    STAGE_DEFINITION = "stage_definition"
    # Stage codes, DAS ranges, NDVI trend per stage

    IRRIGATION_PARAMETERS = "irrigation_parameters"
    # Kc values per stage, MAD, root depth, crop-specific irrigation rules

    FERTIGATION_SCHEDULE = "fertigation_schedule"
    # Timing + doses for fertilizer / nutrient application; INM guidelines

    IPM_SCHEDULE = "ipm_schedule"
    # Pest & disease preventive CALENDAR: fixed-timing spray/trap schedule, runs
    # regardless of current field state. Child #1 of the "Pest & Disease Advisory"
    # parent section.

    PEST_DISEASE_CONDITION_RULE = "pest_disease_condition_rule"
    # Pest & disease TRIGGER rules: if-this-then-that rules keyed on crop +
    # DAS/growth_stage + weather thresholds + symptoms. Reactive, depends on
    # live field data. Child #2 of the "Pest & Disease Advisory" parent section.

    YIELD_PARAMETERS = "yield_parameters"
    # Harvest Index, biomass assumptions, yield estimation inputs

    MARKET_DATA = "market_data"
    # Base market price, grade/variety pricing, unit = per quintal

    # ── Extra crop knowledge (multiple can be active per crop) ────────────
    CROP_KNOWLEDGE = "crop_knowledge"
    # Crop-specific logic/rules that don't fit the standard types above.
    # Multiple crop_knowledge docs can be active simultaneously for one crop.
    # Each gets a unique doc_key: {crop}_crop_knowledge_{knowledge_title_slug}

    # ── Shared / supporting types (used anywhere) ─────────────────────────
    CONDITION_RULE = "condition_rule"
    TREATMENT_MAPPING = "treatment_mapping"
    GUARDRAIL = "guardrail"
    AGRONOMIC_KNOWLEDGE = "agronomic_knowledge"
    CROP_PARAMETERS = "crop_parameters"


# The 6 sections every crop must have for "complete" coverage.
# A section can bundle multiple doc_types — it is counted as "present" when
# ANY of its doc_types is present.
#
# Pest & Disease Advisory groups two children:
#   - ipm_schedule              (preventive calendar)
#   - pest_disease_condition_rule (if-this-then-that triggers)
# Either one marks the section present.
REQUIRED_COVERAGE_SECTIONS: list[dict] = [
    {"key": "stage_definition",
     "label": "Crop Stage Definition",
     "doc_types": [DocType.STAGE_DEFINITION]},
    {"key": "irrigation_parameters",
     "label": "Irrigation Parameters (Kc, MAD, root depth)",
     "doc_types": [DocType.IRRIGATION_PARAMETERS]},
    {"key": "fertigation_schedule",
     "label": "Fertilizer / Nutrient Schedule (INM)",
     "doc_types": [DocType.FERTIGATION_SCHEDULE]},
    {"key": "pest_disease_advisory",
     "label": "Pest & Disease Advisory",
     "doc_types": [DocType.IPM_SCHEDULE, DocType.PEST_DISEASE_CONDITION_RULE]},
    {"key": "yield_parameters",
     "label": "Yield Estimation Parameters (HI, Biomass)",
     "doc_types": [DocType.YIELD_PARAMETERS]},
    {"key": "market_data",
     "label": "Financial / Market Data",
     "doc_types": [DocType.MARKET_DATA]},
]

# Per-child labels (shown as sub-rows under grouped sections).
CHILD_DOC_TYPE_LABELS: dict[DocType, str] = {
    DocType.IPM_SCHEDULE:                 "IPM Schedule (preventive calendar)",
    DocType.PEST_DISEASE_CONDITION_RULE:  "Pest & Disease Condition Rules (triggers)",
}


class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Source(str, Enum):
    CLIENT_UPLOAD = "client_upload"
    EXPERT = "expert"
    ICAR = "icar"


# ---------------------------------------------------------------------------
# Pipeline intermediate shapes
# ---------------------------------------------------------------------------


class Classification(BaseModel):
    """Output of the LLM Classification Layer."""

    engine: Engine
    crop: str
    doc_type: DocType
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class DocumentMetadata(BaseModel):
    """Metadata attached to a stored document. Every field here is filterable."""

    doc_id: str
    doc_key: str
    engine: Engine
    type: DocType
    crop: str
    version: int
    is_active: bool
    priority: Priority = Priority.MEDIUM
    source: Source = Source.CLIENT_UPLOAD
    description: str


class StoredDocument(BaseModel):
    """Full document as stored in the vector DB (metadata + body)."""

    metadata: DocumentMetadata
    body: dict[str, Any]
    text_for_embedding: str


# ---------------------------------------------------------------------------
# Pending state shapes (what lives in the in-memory WAIT store)
# ---------------------------------------------------------------------------


class PendingClassification(BaseModel):
    upload_id: str
    created_at: datetime
    raw_text: str
    raw_structured: Optional[Any] = None  # parsed JSON dict or list-of-row-dicts; None for PDF
    possible_types: list[str]
    classification: Classification
    original_filename: str


class PendingUpload(BaseModel):
    upload_id: str
    created_at: datetime
    validated_doc: dict[str, Any]
    metadata: DocumentMetadata
    text_for_embedding: str
    existing_doc_id: Optional[str] = None
    existing_version: Optional[int] = None
    # Evidence review fields — populated when LLM inferred values without source
    flagged_fields: list[str] = []
    requires_evidence_review: bool = False


# ---------------------------------------------------------------------------
# Coverage response shapes
# ---------------------------------------------------------------------------


class CoverageChild(BaseModel):
    """One child doc_type under a grouped section (e.g. IPM under Pest & Disease)."""
    doc_type: str
    label: str
    status: Literal["present", "missing"]
    doc_key: Optional[str] = None
    doc_id: Optional[str] = None
    version: Optional[int] = None
    description: Optional[str] = None


class CoverageItem(BaseModel):
    """Status of one required section for a crop. Present if ANY child is present."""
    section_key: str
    label: str
    status: Literal["present", "missing"]
    # For single-doc_type sections, these top-level fields mirror the one child.
    # For grouped sections (e.g. Pest & Disease Advisory), these mirror the first
    # present child (or stay None if all missing); children[] carries the full list.
    doc_type: Optional[str] = None
    doc_key: Optional[str] = None
    doc_id: Optional[str] = None
    version: Optional[int] = None
    description: Optional[str] = None
    children: list[CoverageChild] = []  # always populated; one entry per child doc_type


class CropCoverageResponse(BaseModel):
    crop: str
    collection: str
    completeness_pct: int           # 0-100, % of required sections present
    required: list[CoverageItem]    # one entry per REQUIRED_COVERAGE_SECTIONS
    extra_knowledge: list[dict]     # all active crop_knowledge docs


# ---------------------------------------------------------------------------
# API request / response shapes
# ---------------------------------------------------------------------------


class UploadStoredResponse(BaseModel):
    status: Literal["stored"] = "stored"
    upload_id: str
    doc_key: str
    version: int


class UploadPendingClassificationResponse(BaseModel):
    status: Literal["pending_classification"] = "pending_classification"
    upload_id: str
    predicted: Classification
    options: list[str] = ["approve", "reject"]


class UploadPendingVersionResponse(BaseModel):
    status: Literal["pending_version"] = "pending_version"
    upload_id: str
    doc_key: str
    existing_version: int
    message: str
    options: list[str] = ["replace", "reject"]


class UploadPendingEvidenceResponse(BaseModel):
    status: Literal["pending_evidence_review"] = "pending_evidence_review"
    upload_id: str
    doc_key: str
    flagged_fields: list[str]
    message: str
    options: list[str] = ["approve", "reject"]


class ConfirmClassifyRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ConfirmVersionRequest(BaseModel):
    decision: Literal["replace", "reject"]


class ConfirmEvidenceRequest(BaseModel):
    decision: Literal["approve", "reject"]


class StoppedResponse(BaseModel):
    status: Literal["stopped", "rejected"]
    upload_id: str


class StatusResponse(BaseModel):
    upload_id: str
    status: str
    expires_in_minutes: Optional[int] = None


# ---------------------------------------------------------------------------
# Error format — every pipeline block that fails returns this shape
# ---------------------------------------------------------------------------


class PipelineErrorResponse(BaseModel):
    """
    Standard error shape. The `block` field names the exact Excalidraw box
    so anyone reading a failure can point to it on the diagram.
    """

    error: Literal[True] = True
    block: str
    reason: str
    detail: Optional[str] = None
    action_required: str


class PipelineError(Exception):
    """
    Raised anywhere inside pipeline/ when a block fails.

    The block name must match the name on the Excalidraw diagram exactly.
    """

    def __init__(
        self,
        block: str,
        reason: str,
        action_required: str,
        detail: Optional[str] = None,
    ):
        self.block = block
        self.reason = reason
        self.detail = detail
        self.action_required = action_required
        super().__init__(f"[{block}] {reason}")

    def to_response(self) -> PipelineErrorResponse:
        return PipelineErrorResponse(
            block=self.block,
            reason=self.reason,
            detail=self.detail,
            action_required=self.action_required,
        )
