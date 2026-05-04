"""
POST /farm-advisory

Production endpoint: take a farm_id, fetch everything we need from
Supabase + weather + soil, then run the 6-engine RAG pipeline.

WHY this lives next to /advisory (not replacing it):
  /advisory takes a hand-built AdvisoryContext. That stays — it is the
  contract for testing engines in isolation, evaluating prompts, and
  exercising specific edge cases without a real farm in the database.
  /farm-advisory is the production caller: one ID in, full advisory out.

Pipeline:
  1. Supabase: farm profile (location, crop, sowing_date, loan, market price).
  2. Parallel fetch: Open-Meteo weather + SoilGrids soil.
  3. Build AdvisoryContext:
       - crop / sowing_date come from the farm record
       - weather, soil, satellite go into their typed slots
       - extra carries the farm metadata the engines need to reason
         (farm_id, location, irrigation method, loan, market price)
  4. Hand off to generate_advisories — the orchestrator does NOT need to
     know anything about Supabase / weather APIs / etc.
"""

import asyncio
from datetime import date as _date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.advisory import generate_advisories
from app.advisory.context import AdvisoryContext
from app.data_fetchers import get_farm_profile, get_soil_data, get_weather_features

router = APIRouter()


# Demo-mode tree_count map. Replace with a real `farms.tree_count` column
# when the migration runs. Default density assumes traditional spacing
# (~109 trees/acre); high-density orchards run 400–1000 trees/acre.
_DEMO_TREE_COUNTS: dict[str, int] = {
    "APPLE_DEMO_001": 109,
    "APPLE_DEMO_002": 109,
    "APPLE_DEMO_003": 109,
}
_DEFAULT_TREES_PER_ACRE = 109


class SatellitePoint(BaseModel):
    date: str
    value: float


class SatelliteData(BaseModel):
    ndvi_timeseries: list[SatellitePoint] = []
    evi_timeseries: list[SatellitePoint] = []
    ndwi_timeseries: list[SatellitePoint] = []


class FarmAdvisoryRequest(BaseModel):
    farm_id: str
    language: Optional[str] = None
    current_date: Optional[str] = None        # YYYY-MM-DD; defaults to today UTC
    satellite_data: Optional[SatelliteData] = None
    iot_soil_moisture_mm: Optional[float] = None
    pre_fetched_soil: Optional[dict[str, Any]] = None  # bypasses SoilGrids fetch
    # Default raised 1 → 3 so when E4 carries multiple doc_types
    # (pest_disease_condition_rule + ipm_schedule when 4.2 lands), retrieval
    # pulls all of them in one call. k is an UPPER bound — single-doc engines
    # (E1/E3/E5/E6) just return the one doc they have.
    k: int = Field(default=3, ge=1, le=10)


def _today_utc() -> str:
    return datetime.utcnow().date().isoformat()


def _parse_date(s: str) -> _date:
    return _date.fromisoformat(s)


@router.post("/farm-advisory", tags=["advisory"])
async def farm_advisory(req: FarmAdvisoryRequest):
    current_date_str = req.current_date or _today_utc()

    # ── 1) Farm profile (required) ─────────────────────────────────────────
    try:
        farm = get_farm_profile(req.farm_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        # Supabase down / creds missing → 502, not 500. Distinguishes
        # "we couldn't reach the data layer" from "our code blew up."
        raise HTTPException(status_code=502, detail=str(exc))

    if not farm.get("crop_type") or not farm.get("sowing_date"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Farm '{req.farm_id}' has no active crop_seasons row "
                "(crop_type and sowing_date are required). Seed crop_seasons "
                "in Supabase before calling /farm-advisory."
            ),
        )

    crop = (farm.get("crop_type") or "").lower()
    sowing_date = farm["sowing_date"]
    lat = farm.get("latitude")
    lon = farm.get("longitude")
    language = req.language or farm.get("language") or "English"

    # ── 2) Fetch weather (soil disabled for demo) ──────────────────────────
    # SoilGrids fetch was the dominant tail-latency in /farm-advisory
    # (10-60s on the free tier). Disabled for the demo so the request
    # finishes quickly. To re-enable, restore the asyncio.gather block
    # below and the SoilGrids guard. Callers can still pass
    # `pre_fetched_soil` in the request body to inject soil manually.
    fetch_errors: dict[str, str] = {}

    def _fetch_weather():
        if lat is None or lon is None:
            raise RuntimeError("Farm has no latitude/longitude — cannot fetch weather")
        return get_weather_features(lat, lon)

    weather: Optional[dict] = None
    soil: Optional[dict] = req.pre_fetched_soil  # honour explicit override only
    try:
        weather = await asyncio.to_thread(_fetch_weather)
    except Exception as exc:
        fetch_errors["weather_fetch"] = f"{type(exc).__name__}: {exc}"

    # ── 3) Build AdvisoryContext ───────────────────────────────────────────
    satellite_payload: Optional[dict] = None
    if req.satellite_data is not None:
        satellite_payload = req.satellite_data.model_dump()

    # `extra` is the catch-all the LLM sees verbatim. Anything that doesn't
    # fit the typed slots (farm metadata, loan, language preference, IoT
    # readings) goes here so engines can use it without us inventing new
    # context fields per signal.
    extra: dict[str, Any] = {
        "farm_id": farm.get("farm_id"),
        "farmer_id": farm.get("farmer_id"),
        "farm_name": farm.get("farm_name"),
        "language": language,
        "location": {
            "latitude": lat,
            "longitude": lon,
            "district": farm.get("district"),
            "state": farm.get("state"),
            "country": farm.get("country"),
        },
        "irrigation_method": farm.get("irrigation_method"),
        "pump_flow_rate_lph": farm.get("pump_flow_rate_lph"),
        "farm_area_acres": farm.get("farm_area_acres"),
        # Geometry needed by the live Sentinel Hub adapter (E3 + E5).
        # farm_polygon comes straight from Supabase when populated; lat/lon
        # + farm_area_m2 let the adapter synthesize a bbox when polygon is NULL.
        "farm_polygon": farm.get("farm_polygon"),
        "farm_area_m2": farm.get("farm_area_m2"),
        "outstanding_loan_amount": farm.get("outstanding_loan_amount"),
        "market_price_per_kg": farm.get("market_price_per_kg"),
        "input_cost_invested": farm.get("input_cost_invested"),
        "past_repayment_behavior": farm.get("past_repayment_behavior"),
        "expected_harvest_date": farm.get("expected_harvest_date"),
        "iot_soil_moisture_mm": req.iot_soil_moisture_mm,
        # tree_count is required by E4.2 (IPM cure schedule) to scale per-tree
        # and per-spray-volume doses to the actual orchard size. The Supabase
        # `farms` table does not yet have a `tree_count` column, so we read it
        # from a per-farm demo map here with a sensible default. To migrate to
        # the column, run `ALTER TABLE farms ADD COLUMN tree_count INT;` then
        # replace this lookup with `farm.get("tree_count")`.
        "tree_count": _DEMO_TREE_COUNTS.get(
            farm.get("farm_id"),
            _DEFAULT_TREES_PER_ACRE * (farm.get("farm_area_acres") or 1),
        ),
        # E5 yield-calculation inputs. Read from the farms record if present;
        # otherwise apple-orchard defaults so the engine produces a base yield
        # number for the UI even on farms missing these columns.
        "radius_of_tree": farm.get("radius_of_tree") or 0.10,
        "crop_density": farm.get("crop_density") or 2000,
        "average_fruit_weight_g": farm.get("average_fruit_weight_g") or 150,
        # Orchard altitude in feet — OPTIONAL. If the farms record carries an
        # explicit altitude column we pass it through; otherwise we leave it
        # absent and let E1 (stage) infer altitude from the district/state in
        # `location`. The LLM has reliable priors for the apple-growing belts
        # of HP / J&K / Uttarakhand, so a free-text place name is enough.
        "altitude_ft": (
            farm.get("altitude_ft")
            or farm.get("elevation_ft")
            or (
                round(float(farm["elevation_m"]) * 3.28084)
                if farm.get("elevation_m") is not None
                else None
            )
        ),
    }

    try:
        ctx = AdvisoryContext(
            crop=crop,
            sowing_date=_parse_date(sowing_date),
            current_date=_parse_date(current_date_str),
            weather=weather,
            soil=soil,
            satellite=satellite_payload,
            extra=extra,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            content={"error": True, "message": f"Invalid context: {exc}"},
        )

    # ── 4) Run RAG pipeline ────────────────────────────────────────────────
    advisory = await generate_advisories(ctx, k=req.k)

    # Echo what we resolved so the UI can show the farmer "this is the data
    # we used." Keeps the response self-explanatory without a second round-trip.
    return {
        **advisory,
        "farm": {
            "farm_id": farm.get("farm_id"),
            "farm_name": farm.get("farm_name"),
            "crop": crop,
            "sowing_date": sowing_date,
            "expected_harvest_date": farm.get("expected_harvest_date"),
            "language": language,
            "location": extra["location"],
            "irrigation_method": farm.get("irrigation_method"),
            "outstanding_loan_amount": farm.get("outstanding_loan_amount"),
            "market_price_per_kg": farm.get("market_price_per_kg"),
        },
        "fetch_errors": fetch_errors or None,
    }
