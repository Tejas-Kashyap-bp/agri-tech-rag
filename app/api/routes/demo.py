"""
Controlled-demo endpoints (POST /engine/*).

Each endpoint accepts ONLY predefined dropdown values (Literal-typed Pydantic
fields), assembles a deterministic AdvisoryContext, and runs ONE advisory
engine. Inter-engine dependencies are NOT chained — instead, the upstream
output is supplied directly as a dropdown value (e.g. /engine/fertilizer takes
`crop_stage` rather than calling E1). This keeps the demo predictable and
fast: one LLM round-trip per request, no Supabase / weather / Sentinel Hub
calls.

NOTE: this is a demo surface, not the production /farm-advisory path. Live
data integrations (SoilGrids, Open-Meteo, Sentinel Hub) are intentionally
bypassed so the request is reproducible and offline-safe.
"""

from datetime import date, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.advisory.context import AdvisoryContext
from app.advisory.engines import ENGINES, EngineSpec
from app.advisory.generator import generate_for_engine
from app.advisory.orchestrator import (
    PER_ENGINE_TIMEOUT_S,
    _decorate_with_nutrition_guardrails,
    _decorate_with_satellite_advisory,
    _decorate_with_yield_calculation,
)

router = APIRouter()

CROP = "apple"
# Sown ~3 years ago — apple is perennial; sowing date here is just a stable
# anchor for DAS and does not drive the demo outputs.
DEFAULT_SOWING = date(2023, 3, 15)


# ── shared dropdown vocab ───────────────────────────────────────────────────

# Calendar buckets (E1 driver). Twelve months collapse into five distinct
# advisory states because consecutive months inside the same stage window
# produce the same advisory — the LLM has no extra information to act on.
# Each label maps to a representative day-of-year that lands squarely inside
# its stage window per apple_stage_definition (HP/J&K baseline at ~5,000 ft).
MonthBucket = Literal[
    "Dec–Feb (Dormant)",
    "March (Bud Break)",
    "Apr–May (Flowering)",
    "Jun–Aug (Fruit Dev.)",
    "Sep–Nov (Maturity)",
]
_MONTH_BUCKET_DATE: dict[str, tuple[int, int]] = {
    "Dec–Feb (Dormant)":     (1, 15),   # mid-January
    "March (Bud Break)":     (3, 20),   # bud-break window
    "Apr–May (Flowering)":   (5, 1),    # peak bloom
    "Jun–Aug (Fruit Dev.)":  (7, 15),   # fruit development
    "Sep–Nov (Maturity)":    (10, 1),   # harvest / maturity
}

Altitude = Literal["1000 ft", "3000 ft", "6000 ft"]
_ALTITUDE_FT = {"1000 ft": 1000, "3000 ft": 3000, "6000 ft": 6000}

# Apple growth stages exposed as a dropdown for downstream engines. Names are
# the farmer-friendly labels used in apple_stage_definition.
CropStage = Literal[
    "Dormant",
    "Bud Break",
    "Flowering",
    "Fruit Set",
    "Fruit Development",
    "Maturity",
    "Post-Harvest",
]

SoilHealth = Literal["Low SOC", "Normal", "High SOC"]
FieldCondition = Literal["Healthy", "Moderate Stress", "Severe Stress"]

Temperature = Literal["Cool (15°C)", "Mild (22°C)", "Warm (28°C)"]
_TEMP_C = {"Cool (15°C)": 15.0, "Mild (22°C)": 22.0, "Warm (28°C)": 28.0}

Humidity = Literal["Dry (40%)", "Moderate (70%)", "Humid (95%)"]
_HUM_PCT = {"Dry (40%)": 40.0, "Moderate (70%)": 70.0, "Humid (95%)": 95.0}

# Conducive duration is the run-length (hours) where weather stays inside a
# pest's temp+RH band. Each rule has BOTH a min and max — picking 48 h busts
# the upper bound for short-window pests like Apple Scab (9–16 h at S2).
# Exposing duration as an input lets the demo land inside any rule's window.
Duration = Literal["Short (8 h)", "Medium (12 h)", "Long (24 h)", "Very Long (48 h)"]
_DUR_HRS = {"Short (8 h)": 8, "Medium (12 h)": 12, "Long (24 h)": 24, "Very Long (48 h)": 48}

# Canopy density dropdown for the pest-risk demo. NDVI values are chosen so
# the LAI proxy (LAI ≈ -ln(1 - NDVI) / 0.5) lands cleanly inside each canopy
# bucket consumed by lai_biomass_scab_guardrail (LOW <2, MEDIUM 2–<4, HIGH ≥4):
#   NDVI 0.40 → LAI≈1.02  (LOW)
#   NDVI 0.70 → LAI≈2.41  (MEDIUM)
#   NDVI 0.88 → LAI≈4.24  (HIGH)
# "Unknown" intentionally omits NDVI so the guardrail reports canopy=UNKNOWN.
Canopy = Literal["Unknown", "Sparse (NDVI 0.40)", "Moderate (NDVI 0.70)", "Dense (NDVI 0.88)"]
_NDVI_FOR_CANOPY: dict[str, Optional[float]] = {
    "Unknown": None,
    "Sparse (NDVI 0.40)": 0.40,
    "Moderate (NDVI 0.70)": 0.70,
    "Dense (NDVI 0.88)": 0.88,
}

TreeCount = Literal["100", "200", "500"]

TriggeredOrganism = Literal[
    "None", "Apple Scab", "Codling Moth", "San Jose Scale", "Powdery Mildew",
]


def _date_for_bucket(bucket: MonthBucket) -> date:
    """Each calendar bucket maps to a representative date inside its stage
    window. Anchoring to a representative day (not just month=15) keeps the
    request deterministic AND ensures the chosen date falls cleanly inside
    one stage window rather than straddling two."""
    today = date.today()
    m, d = _MONTH_BUCKET_DATE[bucket]
    return date(today.year, m, d)


def _spec(engine_id: str) -> EngineSpec:
    for s in ENGINES:
        if s.engine_id == engine_id:
            return s
    raise KeyError(engine_id)


def _stage_upstream(stage: CropStage) -> dict[str, dict[str, Any]]:
    """Synthesize a minimal e1_stage upstream payload from a dropdown choice.
    The downstream engine prompt only reads `summary` + `details` from
    upstream (see generator._build_prompt), so this is the entire surface we
    need to mock."""
    return {
        "e1_stage": {
            "summary": f"The orchard is in the {stage} stage.",
            "details": {
                "current_stage": stage,
                "reasoning": "Stage supplied directly as a demo input.",
            },
        }
    }


def _risk_upstream(triggered: TriggeredOrganism) -> dict[str, Any]:
    """Synthesize an e4_pest_disease_risk upstream payload from the chosen
    organism. Empty `triggered_organisms` means E4.2 falls back to the full
    preventive block for the stage (per its prompt)."""
    organisms = [] if triggered == "None" else [triggered]
    return {
        "summary": (
            f"{triggered} is currently flagged as a triggered risk."
            if organisms
            else "No organism has crossed its risk threshold; preventive cover only."
        ),
        "details": {
            "triggered_organisms": organisms,
            "reasoning": "Triggered organism supplied directly as a demo input.",
        },
    }


def _build_satellite_for_field_condition(condition: FieldCondition) -> dict[str, float]:
    """Map a coarse field-condition dropdown to NDVI/NDRE/EVI numbers in the
    bands the satellite/yield decorators classify on. Pre-populating
    context.satellite means the live Sentinel Hub fetch in
    orchestrator._enrich_context_with_live_satellite would only fill gaps
    (setdefault) — but we route around the orchestrator entirely so the live
    fetch never runs in demo mode."""
    if condition == "Healthy":
        return {"ndvi_current": 0.78, "ndvi_delta_7d": 0.03,
                "ndre_current": 0.42, "evi_current": 0.55,
                "source": "demo-controlled"}
    if condition == "Moderate Stress":
        return {"ndvi_current": 0.55, "ndvi_delta_7d": -0.02,
                "ndre_current": 0.28, "evi_current": 0.38,
                "source": "demo-controlled"}
    return {"ndvi_current": 0.32, "ndvi_delta_7d": -0.08,
            "ndre_current": 0.18, "evi_current": 0.22,
            "source": "demo-controlled"}


def _build_soil_for_health(soil_health: SoilHealth) -> dict[str, Any]:
    if soil_health == "Low SOC":
        return {"soc_pct": 0.3, "ph": 5.6, "interpretation": "low organic carbon"}
    if soil_health == "High SOC":
        return {"soc_pct": 1.8, "ph": 6.5, "interpretation": "high organic carbon"}
    return {"soc_pct": 0.9, "ph": 6.2, "interpretation": "typical"}


def _build_weather_for_pest(
    temp: Temperature, humidity: Humidity, duration: Duration
) -> dict[str, Any]:
    """Hourly arrays at constant temp/RH for `duration` hours. Drives the
    pest-rule conducive-duration computation predictably. The 48 h hourly
    window is preserved so downstream consumers see a full 2-day series; only
    the conducive_duration_hrs summary value reflects the chosen duration."""
    t = _TEMP_C[temp]
    rh = _HUM_PCT[humidity]
    hours = 48
    dur = _DUR_HRS[duration]
    return {
        "hourly": {
            "temperature_2m": [t] * hours,
            "relative_humidity_2m": [rh] * hours,
            "precipitation": [0.0] * hours,
            "wind_speed_10m": [3.0] * hours,
        },
        "summary": {
            "temperature_c": t,
            "humidity_pct": rh,
            "conducive_duration_hrs": dur,
        },
    }


def _run(
    spec: EngineSpec,
    context: AdvisoryContext,
    upstream_outputs: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Single-engine runner used by every demo endpoint. Bypasses
    orchestrator._run_engine because:
      1. We never want the live Sentinel Hub fetch in demo mode.
      2. We never want guardrail decoration (which depends on real weather
         shape) — keeping the response shape simple for the UI.
    Decorators that ARE useful for the demo (E3 satellite/nutrition copy,
    E5 yield calculation) are applied explicitly below."""
    result = generate_for_engine(
        context, spec, k=3,
        timeout=PER_ENGINE_TIMEOUT_S,
        upstream_outputs=upstream_outputs,
    )
    if spec.engine_id == "e3_nutrition":
        _decorate_with_satellite_advisory(result, context)
        _decorate_with_nutrition_guardrails(result, context)
    if spec.engine_id == "e5_yield":
        _decorate_with_yield_calculation(result, context)
    if spec.engine_id == "e4_pest_disease_risk":
        # One source of truth for apple-scab post-processing — the same
        # decorator the production /farm-advisory orchestrator uses. Pure
        # post-LLM string + dict mutation, NO extra LLM call. We pass an
        # empty dict for the E4.2 slot because this demo endpoint only
        # returns E4.1; the decorator writes E4.2 fields into that dict
        # which we then discard.
        try:
            from app.advisory.guardrails import decorate_with_guardrails
            decorate_with_guardrails(
                e41_result=result,
                e42_result={"details": {}},
                weather=context.weather,
                context_extra=context.extra,
                current_date=context.current_date,
                e1_summary=(upstream_outputs or {}).get("e1_stage", {}).get("summary"),
            )
        except Exception:
            import logging
            logging.getLogger("demo.pest_risk").warning(
                "apple_scab_decoration_failed", exc_info=True,
            )
    return {
        **result,
        "status": "ok" if result.get("parse_status") != "error" else "error",
    }


# ── endpoint 1: crop stage (E1) ─────────────────────────────────────────────

class CropStageRequest(BaseModel):
    month: MonthBucket
    altitude: Altitude


@router.post("/engine/crop-stage", tags=["demo"])
def demo_crop_stage(req: CropStageRequest):
    ctx = AdvisoryContext(
        crop=CROP,
        sowing_date=DEFAULT_SOWING,
        current_date=_date_for_bucket(req.month),
        extra={
            "altitude_ft": _ALTITUDE_FT[req.altitude],
            "location": {"district": "Shimla", "state": "Himachal Pradesh",
                         "country": "India"},
        },
    )
    out = _run(_spec("e1_stage"), ctx)
    return {"inputs": req.model_dump(), "output": out}


# ── endpoint 2: fertilizer (E3) ─────────────────────────────────────────────

class FertilizerRequest(BaseModel):
    crop_stage: CropStage
    soil_health: SoilHealth
    field_condition: FieldCondition


@router.post("/engine/fertilizer", tags=["demo"])
def demo_fertilizer(req: FertilizerRequest):
    ctx = AdvisoryContext(
        crop=CROP,
        sowing_date=DEFAULT_SOWING,
        current_date=date.today(),
        soil=_build_soil_for_health(req.soil_health),
        satellite=_build_satellite_for_field_condition(req.field_condition),
        extra={"farm_area_acres": 1.0, "tree_count": 109},
    )
    out = _run(_spec("e3_nutrition"), ctx, _stage_upstream(req.crop_stage))
    return {"inputs": req.model_dump(), "output": out}


# ── endpoint 3: pest & disease risk (E4.1) ──────────────────────────────────

class PestRiskRequest(BaseModel):
    crop_stage: CropStage
    temperature: Temperature
    humidity: Humidity
    duration: Duration
    # Optional with a safe default so existing callers / saved harnesses
    # without a `canopy` field continue to work unchanged.
    canopy: Canopy = "Unknown"


@router.post("/engine/pest-risk", tags=["demo"])
def demo_pest_risk(req: PestRiskRequest):
    extra: dict[str, Any] = {"farm_area_acres": 1.0, "tree_count": 109}
    ndvi = _NDVI_FOR_CANOPY.get(req.canopy)
    if ndvi is not None:
        # Pass NDVI through extra.satellite so _extract_lai's NDVI proxy
        # (LAI ≈ -ln(1 - NDVI) / 0.5) fires inside the LAI biomass guardrail.
        # No live Sentinel Hub call — this is a demo-controlled value.
        extra["satellite"] = {"ndvi": ndvi, "source": "demo-controlled"}
    ctx = AdvisoryContext(
        crop=CROP,
        sowing_date=DEFAULT_SOWING,
        current_date=date.today(),
        weather=_build_weather_for_pest(req.temperature, req.humidity, req.duration),
        extra=extra,
    )
    out = _run(_spec("e4_pest_disease_risk"), ctx, _stage_upstream(req.crop_stage))
    return {"inputs": req.model_dump(), "output": out}


# ── endpoint 4: IPM cure schedule (E4.2) ────────────────────────────────────

class IpmRequest(BaseModel):
    crop_stage: CropStage
    tree_count: TreeCount
    triggered_organism: TriggeredOrganism


@router.post("/engine/ipm", tags=["demo"])
def demo_ipm(req: IpmRequest):
    trees = int(req.tree_count)
    ctx = AdvisoryContext(
        crop=CROP,
        sowing_date=DEFAULT_SOWING,
        current_date=date.today(),
        extra={
            "tree_count": trees,
            "farm_area_acres": round(trees / 109, 2),
        },
    )
    upstream = {
        **_stage_upstream(req.crop_stage),
        "e4_pest_disease_risk": _risk_upstream(req.triggered_organism),
    }
    out = _run(_spec("e4_2_pest_disease_cure"), ctx, upstream)
    return {"inputs": req.model_dump(), "output": out}


# ── endpoint 5: yield (E5) ──────────────────────────────────────────────────

class YieldRequest(BaseModel):
    crop_stage: CropStage
    tree_count: TreeCount
    field_condition: FieldCondition


@router.post("/engine/yield", tags=["demo"])
def demo_yield(req: YieldRequest):
    trees = int(req.tree_count)
    ctx = AdvisoryContext(
        crop=CROP,
        sowing_date=DEFAULT_SOWING,
        current_date=date.today(),
        satellite=_build_satellite_for_field_condition(req.field_condition),
        extra={
            "tree_count": trees,
            "farm_area_acres": round(trees / 109, 2),
            "radius_of_tree": 0.10,
            "crop_density": 2000,
            "average_fruit_weight_g": 150,
        },
    )
    out = _run(_spec("e5_yield"), ctx, _stage_upstream(req.crop_stage))
    return {"inputs": req.model_dump(), "output": out}
