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
    k: int = Field(default=1, ge=1, le=10)


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

    # ── 2) Parallel fetch: weather + soil ──────────────────────────────────
    fetch_errors: dict[str, str] = {}

    state = farm.get("state")
    if not state:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Farm '{req.farm_id}' has no `state` on record — required "
                "for soil lookup. Update the farm record before calling."
            ),
        )

    def _fetch_weather():
        if lat is None or lon is None:
            raise RuntimeError("Farm has no latitude/longitude — cannot fetch weather")
        return get_weather_features(lat, lon)

    def _fetch_soil():
        if req.pre_fetched_soil is not None:
            return req.pre_fetched_soil
        return get_soil_data(
            report_path=None,
            lat=lat,
            lon=lon,
            state=state,
            district=farm.get("district"),
            soil_type=farm.get("soil_type") or "Loam",
            crop=crop,
            priority_order=None,
        ).to_dict()

    weather: Optional[dict] = None
    soil: Optional[dict] = None
    weather_result, soil_result = await asyncio.gather(
        asyncio.to_thread(_fetch_weather),
        asyncio.to_thread(_fetch_soil),
        return_exceptions=True,
    )
    if isinstance(weather_result, Exception):
        fetch_errors["weather_fetch"] = f"{type(weather_result).__name__}: {weather_result}"
    else:
        weather = weather_result
    if isinstance(soil_result, Exception):
        fetch_errors["soil_fetch"] = f"{type(soil_result).__name__}: {soil_result}"
    else:
        soil = soil_result

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
        "outstanding_loan_amount": farm.get("outstanding_loan_amount"),
        "market_price_per_kg": farm.get("market_price_per_kg"),
        "input_cost_invested": farm.get("input_cost_invested"),
        "past_repayment_behavior": farm.get("past_repayment_behavior"),
        "expected_harvest_date": farm.get("expected_harvest_date"),
        "iot_soil_moisture_mm": req.iot_soil_moisture_mm,
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
