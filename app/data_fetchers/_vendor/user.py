# ---------------------------------------------------------------------------
# data_fetchers/user.py
#
# Farm profile fetcher — reads from Supabase tables seeded by scripts/migrate.sql:
#   farms            — static farm metadata + location + irrigation method
#   crop_seasons     — active crop_type / sowing_date / harvest_date
#   market_finance   — latest outstanding loan + market price (optional)
#
# Returned dict is shaped so it can be merged directly into engine inputs by
# the integrated /farm-advisory endpoint.
# ---------------------------------------------------------------------------

import os
from typing import Optional

import requests

_SUPABASE_URL = os.getenv("SUPABASE_URL")
_SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_ANON_KEY")
    or os.getenv("SUPABASE_KEY")
)

_TIMEOUT = 10
_SESSION = requests.Session()

# Projected columns — narrow the wire payload and avoid leaking unused fields.
_FARM_COLS = (
    "farm_id,farmer_id,farm_name,latitude,longitude,district,state,country,"
    "farm_area_acres,farm_area_m2,farm_polygon,irrigation_type,"
    "pump_flow_rate_lph,soil_type_farmer_reported,language_preference,"
    "past_repayment_behavior"
)
_SEASON_COLS = (
    "season_id,farm_id,crop_type,variety,sowing_date,expected_harvest_date,is_active"
)
_FINANCE_COLS = (
    "season_id,outstanding_loan_amount,market_price_per_kg,input_cost_invested,recorded_at"
)


def _headers() -> dict:
    return {
        "apikey": _SUPABASE_KEY or "",
        "Authorization": f"Bearer {_SUPABASE_KEY or ''}",
        "Content-Type": "application/json",
    }


def _get(table: str, params: dict) -> list:
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        raise RuntimeError(
            "Supabase credentials missing — set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY) in .env"
        )
    url = f"{_SUPABASE_URL}/rest/v1/{table}"
    try:
        r = _SESSION.get(url, headers=_headers(), params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as exc:
        # Translate to RuntimeError so callers (e.g. farm_advisory) catch it cleanly
        # instead of letting a generic 500 + stack trace bubble out.
        raise RuntimeError(f"Supabase request failed for {table}: {exc}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Supabase network error for {table}: {exc}") from exc


def get_farm_profile(farm_id: str) -> dict:
    """
    Fetch full farm profile by farm_id.

    Returns a flat dict combining farms + active crop_seasons row + latest
    market_finance row. Missing-but-optional fields come back as None so the
    downstream endpoint can decide what to skip.

    Raises:
        LookupError: farm_id not found in farms table.
        RuntimeError: Supabase credentials missing or HTTP failure.
    """
    farm_rows = _get("farms", {"farm_id": f"eq.{farm_id}", "select": _FARM_COLS, "limit": "1"})
    if not farm_rows:
        raise LookupError(f"farm_id '{farm_id}' not found in farms table")
    farm = farm_rows[0]

    season = _get_active_season(farm_id)
    finance = _get_latest_finance(season.get("season_id") if season else None)

    return {
        "farm_id":            farm.get("farm_id"),
        "farmer_id":          farm.get("farmer_id"),
        "farm_name":          farm.get("farm_name"),
        "latitude":           farm.get("latitude"),
        "longitude":          farm.get("longitude"),
        "district":           farm.get("district"),
        "state":              farm.get("state"),
        "country":            farm.get("country"),
        "farm_area_acres":    farm.get("farm_area_acres"),
        "farm_area_m2":       farm.get("farm_area_m2"),
        "farm_polygon":       farm.get("farm_polygon"),
        "irrigation_method":  farm.get("irrigation_type"),
        "pump_flow_rate_lph": farm.get("pump_flow_rate_lph"),
        "soil_type":          farm.get("soil_type_farmer_reported"),
        "language":           farm.get("language_preference") or "English",
        "past_repayment_behavior": farm.get("past_repayment_behavior"),  # populated once column exists

        # Active crop season (optional)
        "season_id":          season.get("season_id") if season else None,
        "crop_type":          season.get("crop_type") if season else None,
        "variety":            season.get("variety") if season else None,
        "sowing_date":        season.get("sowing_date") if season else None,
        "expected_harvest_date": season.get("expected_harvest_date") if season else None,

        # Finance (optional)
        "outstanding_loan_amount": finance.get("outstanding_loan_amount") if finance else None,
        "market_price_per_kg":     finance.get("market_price_per_kg") if finance else None,
        "input_cost_invested":     finance.get("input_cost_invested") if finance else None,
    }


def _get_active_season(farm_id: str) -> Optional[dict]:
    rows = _get(
        "crop_seasons",
        {
            "farm_id":   f"eq.{farm_id}",
            "is_active": "eq.true",
            "select":    _SEASON_COLS,
            "order":     "sowing_date.desc",
            "limit":     "1",
        },
    )
    return rows[0] if rows else None


def _get_latest_finance(season_id: Optional[str]) -> Optional[dict]:
    if not season_id:
        return None
    rows = _get(
        "market_finance",
        {
            "season_id": f"eq.{season_id}",
            "select":    _FINANCE_COLS,
            "order":     "recorded_at.desc",
            "limit":     "1",
        },
    )
    return rows[0] if rows else None
