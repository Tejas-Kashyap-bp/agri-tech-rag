"""
UI-facing advisory endpoints.

  POST /ui-advisory                → run the full apple advisory from a
                                     hand-built AdvisoryContext (same body as
                                     POST /advisory). Returned in the same
                                     shape; this exists as a stable surface
                                     the React frontend can target without
                                     entangling itself with Phase-1 dev
                                     contracts on /advisory.

  GET  /ui-advisory/demo/{farm_id} → one-shot demo: build the AdvisoryContext
                                     for {farm_id} from Supabase + Open-Meteo
                                     and run the full advisory in a single
                                     request. Returns both the resolved
                                     context (so the UI can show "Loaded
                                     values") and the engine outputs.

WHY a separate router (not just reusing /farm-advisory):
  /farm-advisory is the production caller and still includes scaffolding (E2,
  formerly E6) the React UI does not surface. /ui-advisory wraps the same
  underlying generator with a contract specifically shaped for the React UI:
    - inputs_used per engine (already added at the orchestrator level)
    - resolved_context echoed at the top level so the UI can render a
      "Loaded values" panel without parsing the per-engine block.

The two endpoints are intentionally thin — the heavy lifting (Supabase fetch,
weather fetch, AdvisoryContext build) is reused from farm_advisory so the two
surfaces cannot drift on what data they pull.
"""

import asyncio
from datetime import date as _date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.advisory import generate_advisories
from app.advisory.context import AdvisoryContext
from app.api.routes.farm_advisory import (
    _DEMO_TREE_COUNTS,
    _DEFAULT_TREES_PER_ACRE,
    SatelliteData,
)
from app.data_fetchers import get_farm_profile, get_weather_features

router = APIRouter()


def _parse_date(s: str) -> _date:
    return _date.fromisoformat(s)


def _today_utc() -> str:
    return datetime.utcnow().date().isoformat()


# ---------------------------------------------------------------------------
# POST /ui-advisory — run the advisory from a hand-built context
# ---------------------------------------------------------------------------


@router.post("/ui-advisory", tags=["ui-advisory"])
async def ui_advisory(context: AdvisoryContext, k: int = 3):
    if k < 1 or k > 10:
        raise HTTPException(status_code=400, detail="k must be between 1 and 10")
    advisory = await generate_advisories(context, k=k)
    return {
        **advisory,
        "resolved_context": _resolved_context_block(context, source="client"),
    }


# ---------------------------------------------------------------------------
# GET /ui-advisory/demo/{farm_id} — Supabase-backed one-shot
# ---------------------------------------------------------------------------


class UIDemoResponse(BaseModel):
    """Echoes the same envelope shape /ui-advisory uses, plus the farm row
    so the React app can render the farmer-profile sidebar without a second
    round-trip."""

    farm: dict[str, Any]
    fetch_errors: Optional[dict[str, str]] = None


@router.get("/ui-advisory/demo/{farm_id}", tags=["ui-advisory"])
async def ui_advisory_demo(farm_id: str, k: int = 3, current_date: Optional[str] = None):
    if k < 1 or k > 10:
        raise HTTPException(status_code=400, detail="k must be between 1 and 10")

    # 1) Farm profile (required) — same as /farm-advisory
    try:
        farm = get_farm_profile(farm_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not farm.get("crop_type") or not farm.get("sowing_date"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Farm '{farm_id}' has no active crop_seasons row "
                "(crop_type and sowing_date are required)."
            ),
        )

    crop = (farm.get("crop_type") or "").lower()
    sowing_date = farm["sowing_date"]
    lat = farm.get("latitude")
    lon = farm.get("longitude")
    current_date_str = current_date or _today_utc()

    # 2) Weather (best-effort; SoilGrids disabled like /farm-advisory)
    fetch_errors: dict[str, str] = {}
    weather: Optional[dict[str, Any]] = None
    if lat is None or lon is None:
        fetch_errors["weather_fetch"] = "Farm has no latitude/longitude"
    else:
        try:
            weather = await asyncio.to_thread(get_weather_features, lat, lon)
        except Exception as exc:
            fetch_errors["weather_fetch"] = f"{type(exc).__name__}: {exc}"

    # 3) Build context — same extras as /farm-advisory so engines see the
    # identical data shape regardless of which UI ran them.
    extra: dict[str, Any] = {
        "farm_id": farm.get("farm_id"),
        "farm_name": farm.get("farm_name"),
        "language": farm.get("language") or "English",
        "location": {
            "latitude": lat,
            "longitude": lon,
            "district": farm.get("district"),
            "state": farm.get("state"),
            "country": farm.get("country"),
        },
        "irrigation_method": farm.get("irrigation_method"),
        "farm_area_acres": farm.get("farm_area_acres"),
        "expected_harvest_date": farm.get("expected_harvest_date"),
        # tree_count required by E4.2 — see farm_advisory.py for the migration
        # plan (replace map lookup with farm.get("tree_count") once column lands).
        "tree_count": _DEMO_TREE_COUNTS.get(
            farm.get("farm_id"),
            _DEFAULT_TREES_PER_ACRE * (farm.get("farm_area_acres") or 1),
        ),
    }

    try:
        ctx = AdvisoryContext(
            crop=crop,
            sowing_date=_parse_date(sowing_date),
            current_date=_parse_date(current_date_str),
            weather=weather,
            extra=extra,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid context: {exc}")

    # 4) Run engines
    advisory = await generate_advisories(ctx, k=k)

    return {
        **advisory,
        "resolved_context": _resolved_context_block(ctx, source="supabase"),
        "farm": {
            "farm_id": farm.get("farm_id"),
            "farm_name": farm.get("farm_name"),
            "crop": crop,
            "sowing_date": sowing_date,
            "expected_harvest_date": farm.get("expected_harvest_date"),
            "language": farm.get("language") or "English",
            "location": extra["location"],
            "irrigation_method": farm.get("irrigation_method"),
            "farm_area_acres": farm.get("farm_area_acres"),
            "tree_count": extra["tree_count"],
        },
        "fetch_errors": fetch_errors or None,
    }


# ---------------------------------------------------------------------------
# Shared helper — top-level resolved_context echo
# ---------------------------------------------------------------------------


def _resolved_context_block(ctx: AdvisoryContext, source: str) -> dict[str, Any]:
    """Compact, UI-friendly snapshot of the context the engines reasoned over.

    `source` distinguishes "client-supplied body" (POST /ui-advisory) from
    "Supabase-fetched" (GET demo). The drawer in the UI shows the source so
    a tester can tell whether a value came from form input vs. a real DB row.
    """
    return {
        "source": source,
        "crop": ctx.crop,
        "sowing_date": ctx.sowing_date.isoformat(),
        "current_date": ctx.current_date.isoformat(),
        "days_after_sowing": ctx.das,
        "weather": ctx.weather,
        "soil": ctx.soil,
        "satellite": ctx.satellite,
        "detection": ctx.detection,
        "extra": ctx.extra,
    }
