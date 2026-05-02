"""
Advisory routes.

  POST /advisory                  → all engines in dependency tiers
                                    (E1, E3, E4.1, E4.2, E5 — E2 and E6 removed for apple)
  POST /advisory/eng1             → just E1 (stage)
  POST /advisory/eng3             → E3 (nutrition) + transparently E1
  POST /advisory/eng4             → E4 (pest & disease risk) + transparently E1
  POST /advisory/eng5             → E5 (yield) + E1

WHY one combined endpoint AND six per-engine endpoints (mirrors agri-integrated):
  Production callers want the full advisory in a single round-trip
  (`/advisory`). Developers and QA want to exercise individual engines in
  isolation (`/advisory/engN`). Per-engine endpoints transparently run any
  upstream engines they depend on so callers don't have to know the
  dependency graph — the response includes an `upstream` map for visibility.
"""

import json
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.advisory import generate_advisories
from app.advisory.context import AdvisoryContext
from app.advisory.orchestrator import generate_single

router = APIRouter()

# Path to the apple pest/disease rule corpus used for the deterministic
# rule-evaluation block in the E4 tweak endpoint. Read once on module import.
_RULES_PATH = (
    Path(__file__).resolve().parents[3]
    / "structured_data" / "apple" / "apple_pest_disease_condition_rule.json"
)
_RULES: list[dict[str, Any]] = []
if _RULES_PATH.exists():
    with _RULES_PATH.open() as _f:
        _RULES = json.load(_f).get("rules", [])


def _validate_k(k: int) -> None:
    if k < 1 or k > 10:
        raise HTTPException(
            status_code=400,
            detail="k must be between 1 and 10",
        )


@router.post("/advisory", tags=["advisory"])
async def advisory(context: AdvisoryContext, k: int = 3):
    _validate_k(k)
    return await generate_advisories(context, k=k)


# Per-engine endpoints — same body shape, single engine output. The path
# segment names match the agri-integrated convention (eng1..eng6) so a
# tester moving between the two systems can keep the same mental model.
_ENGINE_ROUTE_MAP = {
    "eng1": "e1_stage",
    # eng2 (irrigation) removed — perennial tree crop, no daily irrigation engine.
    "eng3": "e3_nutrition",
    "eng4": "e4_pest_disease_risk",
    "eng5": "e5_yield",
}


def _make_engine_route(path_name: str, engine_id: str):
    @router.post(f"/advisory/{path_name}", tags=["advisory"])
    async def _endpoint(context: AdvisoryContext, k: int = 3):
        _validate_k(k)
        return await generate_single(context, engine_id=engine_id, k=k)
    _endpoint.__name__ = f"advisory_{path_name}"
    return _endpoint


for _path, _engine in _ENGINE_ROUTE_MAP.items():
    _make_engine_route(_path, _engine)


# ---------------------------------------------------------------------------
# E4 tweak endpoint
# ---------------------------------------------------------------------------
# A flat-body tester for E4. Lets a developer iterate on (temperature_c,
# humidity_pct, duration_hrs) without rebuilding a full AdvisoryContext each
# time. The endpoint:
#   1. Builds a minimal AdvisoryContext with crop="apple" and a sowing_date
#      anchored to the most recent March 1 (apple's perennial cycle anchor).
#   2. Runs the same E4 generator as /advisory/eng4 (LLM advisory).
#   3. Adds a deterministic `rule_evaluation` block that walks the rule corpus
#      and reports which rules triggered vs. were near-miss for the given
#      weather snapshot — so the LLM advisory can be checked against a
#      ground-truth eval in the same response.
#
# The rule_evaluation block is computed in code (not by the LLM) intentionally:
# this is the verification half of "see if the advisories are correct".
# ---------------------------------------------------------------------------


class E4TweakRequest(BaseModel):
    temperature_c: float = Field(..., description="Live temperature in degrees Celsius")
    humidity_pct: float = Field(..., description="Live relative humidity in percent")
    duration_hrs: float = Field(
        ...,
        description="Hours the conducive condition (warm-humid / leaf-wetness window) has persisted",
    )
    current_date: Optional[_date] = Field(
        default=None,
        description="Calendar date for stage resolution. Defaults to today.",
    )
    organism: Optional[str] = Field(
        default=None,
        description="Optional organism filter (e.g. 'Apple Scab') — narrows rule_evaluation and hints the LLM focus.",
    )


def _stage_for_apple(d: _date) -> dict[str, Any]:
    """Map a calendar date to the apple perennial stage. Code-side derivation
    so the deterministic rule_evaluation block works even if E1 is skipped."""
    mm, dd = d.month, d.day
    key = mm * 100 + dd
    # Bounds match apple_stage_definition.json calendar windows.
    if 301 <= key <= 410:
        return {"stage_code": "S1", "stage_name": "Vegetative"}
    if 411 <= key <= 510:
        return {"stage_code": "S2", "stage_name": "Flowering"}
    if 511 <= key <= 815:
        return {"stage_code": "S3", "stage_name": "Fruiting"}
    if 816 <= key <= 1130:
        return {"stage_code": "S4", "stage_name": "Harvesting"}
    return {"stage_code": "S5", "stage_name": "Dormancy"}


def _evaluate_rules(
    temp_c: float,
    humidity_pct: float,
    duration_hrs: float,
    stage_code: str,
    organism: Optional[str],
) -> dict[str, Any]:
    """Walk the rule corpus and bucket each rule into triggered / near_miss /
    outside for the given weather snapshot. 'near_miss' = exactly one band
    outside; 'triggered' = all three bands inside; 'outside' = 2+ bands out
    (omitted from response to keep payload small)."""
    triggered: list[dict[str, Any]] = []
    near_miss: list[dict[str, Any]] = []
    for rule in _RULES:
        if rule.get("stage_code") != stage_code:
            continue
        if organism and rule.get("organism_name", "").lower() != organism.lower():
            continue
        bands = {
            "temperature_c": (rule["temp_c"]["min"], rule["temp_c"]["max"], temp_c),
            "humidity_pct": (rule["humidity_pct"]["min"], rule["humidity_pct"]["max"], humidity_pct),
            "duration_hrs": (
                rule["conducive_duration_hrs"]["min"],
                rule["conducive_duration_hrs"]["max"],
                duration_hrs,
            ),
        }
        misses: list[dict[str, Any]] = []
        for name, (lo, hi, val) in bands.items():
            if val < lo:
                misses.append({"band": name, "value": val, "min": lo, "max": hi, "gap_below": round(lo - val, 2)})
            elif val > hi:
                misses.append({"band": name, "value": val, "min": lo, "max": hi, "gap_above": round(val - hi, 2)})
        entry = {
            "organism_name": rule["organism_name"],
            "organism_type": rule["organism_type"],
            "stage_code": rule["stage_code"],
            "base_risk_pct": rule["base_risk_pct"],
            "rule_bands": {
                "temp_c": rule["temp_c"],
                "humidity_pct": rule["humidity_pct"],
                "conducive_duration_hrs": rule["conducive_duration_hrs"],
            },
        }
        if not misses:
            triggered.append(entry)
        elif len(misses) == 1:
            entry["band_outside"] = misses[0]
            near_miss.append(entry)
        # 2+ misses → silently skipped to keep the payload focused.
    triggered.sort(key=lambda r: r["base_risk_pct"], reverse=True)
    near_miss.sort(key=lambda r: r["base_risk_pct"], reverse=True)
    return {"triggered": triggered, "near_miss": near_miss}


@router.post("/advisory/eng4/tweak", tags=["advisory"])
async def advisory_eng4_tweak(body: E4TweakRequest, k: int = 3):
    _validate_k(k)

    today = body.current_date or _date.today()

    # Sowing date = most recent March 1 on/before today. Apple is perennial;
    # the stage_definition.json anchors the cycle on March 1, so DAS lands in
    # [0, 364] which is what E1's stage logic expects.
    anchor_year = today.year if today >= _date(today.year, 3, 1) else today.year - 1
    sowing = _date(anchor_year, 3, 1)

    weather = {
        "temperature_c": body.temperature_c,
        "humidity_pct": body.humidity_pct,
        "conducive_duration_hrs": body.duration_hrs,
    }
    extra: dict[str, Any] = {"tweak_mode": True}
    if body.organism:
        extra["organism_focus"] = body.organism

    context = AdvisoryContext(
        crop="apple",
        sowing_date=sowing,
        current_date=today,
        weather=weather,
        extra=extra,
    )

    llm_result = await generate_single(context, engine_id="e4_pest_disease_risk", k=k)

    stage = _stage_for_apple(today)
    rule_eval = _evaluate_rules(
        temp_c=body.temperature_c,
        humidity_pct=body.humidity_pct,
        duration_hrs=body.duration_hrs,
        stage_code=stage["stage_code"],
        organism=body.organism,
    )

    return {
        **llm_result,
        "tweak_inputs": {
            "temperature_c": body.temperature_c,
            "humidity_pct": body.humidity_pct,
            "duration_hrs": body.duration_hrs,
            "current_date": today.isoformat(),
            "organism": body.organism,
        },
        "stage_resolved": stage,
        "rule_evaluation": rule_eval,
    }
