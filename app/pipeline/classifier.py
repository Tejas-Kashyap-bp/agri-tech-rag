"""
Block: LLM Classification Layer

Asks the LLM to determine:
  - which engine the document belongs to
  - which crop it describes
  - which doc_type it is
  - confidence (0-1)
  - short reason explaining the decision

The heuristic prefilter's possible_types are passed in as hints. The LLM is
free to ignore them — they are a nudge, not a constraint.

Confidence threshold handling (the ≥ 0.9 auto-approve rule) lives in the
route layer, not here. This block just returns what the LLM said.
"""

import json

from app.llm.gemini_provider import llm
from app.schemas import Classification, DocType, Engine, PipelineError

BLOCK = "LLM Classification Layer"

_SYSTEM = """You are an agricultural document classifier.
Classify the given document and return a JSON object with exactly these keys:

  engine:     one of [e1_stage, e2_irrigation, e3_nutrition, e4_crop_health, e5_yield, e6_financial]

  crop:       the crop name in lowercase (e.g. "maize", "apple", "sugarcane").
              Use "common" if the document is NOT specific to any one crop —
              for example: fertilizer compositions, micronutrient tables, pump specs,
              general agronomic rules, or data that applies across all crops.

  doc_type:   choose the BEST match from this list:
    stage_definition      — crop growth stages: stage codes, DAS ranges, NDVI trend per stage
    irrigation_parameters — Kc values, MAD, root depth, crop-specific irrigation rules (NOT schedule timing)
    fertigation_schedule  — timing + doses for fertilizer/nutrient application; INM schedule
    ipm_schedule                  — Pest & Disease Advisory (calendar child):
                                    preventive, FIXED-TIMING spray / trap / scouting schedule.
                                    Looks like: "Apply neem oil at 30 DAS", "Install pheromone
                                    traps at tasselling", an IPM calendar table.
    pest_disease_condition_rule   — Pest & Disease Advisory (trigger child):
                                    IF-THIS-THEN-THAT condition rules keyed on crop +
                                    DAS or growth_stage + weather thresholds + symptoms.
                                    Looks like rows with fields such as
                                    rule_id, pest_disease, rule_type (pest|disease), min_das,
                                    max_das, growth_stage, min_temp_c, max_temp_c,
                                    min_humidity_pct, max_humidity_pct, severity, symptoms.
                                    If the data has threshold ranges and symptoms, it is
                                    ALMOST CERTAINLY this type, NOT ipm_schedule.
    yield_parameters      — Harvest Index, biomass estimation inputs, yield assumptions
    market_data           — market price, grade/variety pricing, financial data
    crop_knowledge        — crop-specific logic or rules that do NOT fit any of the above types
    condition_rule        — if/then rules based on sensor or field conditions
    treatment_mapping     — symptom-to-treatment or diagnosis-to-remedy mappings
    guardrail             — hard constraints: do-not rules, safety limits
    agronomic_knowledge   — general agronomy facts not fitting other types
    crop_parameters       — physical/biological crop parameters not fitting other types

  confidence: a float between 0.0 and 1.0
  reason:     a one-sentence explanation of your decision

Return JSON only. No prose, no markdown.
"""


def classify(text: str, possible_types: list[str]) -> Classification:
    hint = (
        f"Heuristic pre-filter suggests these possible types: {possible_types}. "
        f"Use them as hints, not as constraints.\n\n"
        if possible_types
        else ""
    )
    prompt = f"{hint}Document:\n{text}"

    try:
        raw = llm.complete_json(prompt=prompt, system=_SYSTEM)
    except Exception as exc:
        raise PipelineError(
            block=BLOCK,
            reason="LLM API call failed",
            detail=str(exc),
            action_required="Retry the upload or check LLM service status",
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            block=BLOCK,
            reason="LLM returned invalid JSON",
            detail=f"Response: {raw[:500]}",
            action_required="Retry the upload",
        ) from exc

    try:
        return Classification(
            engine=Engine(data["engine"]),
            crop=data["crop"].strip().lower(),
            doc_type=DocType(data["doc_type"]),
            confidence=float(data["confidence"]),
            reason=data["reason"],
        )
    except (KeyError, ValueError) as exc:
        raise PipelineError(
            block=BLOCK,
            reason="LLM classification did not match required schema",
            detail=f"Response: {raw[:500]}",
            action_required="Retry the upload. If this persists, the document may be out of scope.",
        ) from exc
