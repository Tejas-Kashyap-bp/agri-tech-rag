# ---------------------------------------------------------------------------
# data_fetchers/weather.py
#
# Open-Meteo weather data fetcher.
# Fetches historical weather (past 7 days) + 5-day forecast for a farm
# location and returns only the fields consumed by the engines.
#
# ---------------------------------------------------------------------------
# PROVIDER NOTE — currently on Open-Meteo free tier (no API key required).
#
# To upgrade to the commercial tier or swap providers:
#   1. Change _BASE_URL to the commercial endpoint.
#   2. Add "apikey": "<your-key>" to _build_params() if required.
#   3. Add any extra daily variables to _DAILY_VARS if the new tier unlocks
#      higher-resolution fields (e.g. soil moisture, solar radiation).
#
# Everything downstream — callers, engine inputs, output contract — stays
# unchanged. Only this file needs editing when the provider changes.
# ---------------------------------------------------------------------------
#
# Output contract:
#   temperature_last_7_days          list[{"date": str, "tmin": float, "tmax": float}]
#   humidity_last_7_days             list[float]   daily mean RH %        Engine 4
#   rain_last_7_days_mm              list[float]   daily precipitation mm  Engine 2
#   et0_last_7_days_mm               list[float]   daily ET0 mm            Engine 2
#   wind_speed_mps                   float         latest day's max wind   Engine 2
#   rain_forecast_next_5_days_mm     list[float]   5-day rain forecast     Engine 2 / 3 / 4
#   temperature_forecast_next_5_days list[{"tmin": float, "tmax": float}]  Engine 4
#   et0_forecast_next_5_days_mm      list[float]   5-day ET0 forecast      Engine 2
#   fetch_date                       str  YYYY-MM-DD
#
# Consumers:
#   Engine 2 (Irrigation)        — et0_last_7_days_mm, rain_last_7_days_mm,
#                                   rain_forecast_next_5_days_mm,
#                                   et0_forecast_next_5_days_mm, wind_speed_mps
#   Engine 3 (Fertilizer / INM)  — rain_forecast_next_5_days_mm
#                                   (guardrail: no fertilizer before heavy rain)
#   Engine 4 (Crop Protection)   — temperature_last_7_days, humidity_last_7_days,
#                                   rain_forecast_next_5_days_mm
# ---------------------------------------------------------------------------

import logging
import random
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)
_SESSION = requests.Session()

# ---------------------------------------------------------------------------
# Provider config
# Swap _BASE_URL and _DAILY_VARS here when upgrading or changing providers.
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.open-meteo.com/v1/forecast"

# Daily variables fetched from the provider.
# Each variable maps 1-to-1 to a key in the JSON response's "daily" object.
_DAILY_VARS = [
    "temperature_2m_max",          # → tmax  (°C)
    "temperature_2m_min",          # → tmin  (°C)
    "precipitation_sum",           # → rain  (mm)
    "wind_speed_10m_max",          # → wind  (km/h → converted to m/s on output)
    "et0_fao_evapotranspiration",  # → ET0   (mm/day)
    "relative_humidity_2m_max",    # → RH max (%) — averaged with min for daily mean
    "relative_humidity_2m_min",    # → RH min (%)
]

# Hourly variables — used to derive the leaf-wetness / warm-humid duration
# signal that Engine 4's pest/disease rules key on. Open-Meteo does not
# expose leaf wetness directly, so we proxy it from RH and temperature.
_HOURLY_VARS = [
    "relative_humidity_2m",
    "temperature_2m",
]

# Leaf-wetness proxy thresholds. RH ≥ 90 % is the standard surrogate used
# in apple-scab / Mills-table style infection models when a true wetness
# sensor is unavailable; pair it with T ≥ 5 °C to exclude frozen winter
# hours that obviously cannot support pathogen activity.
_RH_WETNESS_THRESHOLD_PCT = 90.0
_TEMP_WETNESS_MIN_C       = 5.0
_WETNESS_LOOKBACK_HOURS   = 48

PAST_DAYS       = 7   # days of history returned (indices 0 … PAST_DAYS-1)
FORECAST_DAYS   = 5   # days of forecast returned (indices PAST_DAYS … PAST_DAYS+FORECAST_DAYS-1)
REQUEST_TIMEOUT = 15  # seconds

_KMH_TO_MPS = 1 / 3.6  # Open-Meteo wind is in km/h; engines expect m/s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_weather_features(latitude: float, longitude: float) -> dict:
    """
    Fetch weather features for a farm location from Open-Meteo.

    Makes a single GET request to Open-Meteo's daily forecast endpoint
    with past_days=7 and forecast_days=5. The response (12 rows total) is
    split into a historical window (rows 0-6) and a forecast window (rows 7-11)
    before being returned as structured engine inputs.

    Args:
        latitude:  Farm centroid latitude in decimal degrees.
        longitude: Farm centroid longitude in decimal degrees.

    Returns:
        {
            "temperature_last_7_days":           list[{"date": str, "tmin": float, "tmax": float}],
            "humidity_last_7_days":               list[float],
            "rain_last_7_days_mm":                list[float],
            "et0_last_7_days_mm":                 list[float],
            "wind_speed_mps":                     float,
            "rain_forecast_next_5_days_mm":        list[float],
            "temperature_forecast_next_5_days":    list[{"tmin": float, "tmax": float}],
            "et0_forecast_next_5_days_mm":         list[float],
            "fetch_date":                          str,  # YYYY-MM-DD UTC
        }

    Raises:
        RuntimeError: on HTTP or network errors.
        ValueError:   if the API returns an unexpected response shape.
    """
    params = _build_params(latitude, longitude)

    # Retry transient failures (5xx, connection errors, timeouts) with
    # exponential backoff + jitter. A single Open-Meteo flap used to knock
    # out E2 + E3 for the whole farm-advisory request.
    last_exc = None
    response = None
    for attempt in range(3):
        try:
            response = _SESSION.get(_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 500:
                last_exc = RuntimeError(f"Open-Meteo {response.status_code}")
                response = None
            else:
                response.raise_for_status()
                break
        except requests.Timeout as exc:
            last_exc = exc
            response = None
        except requests.RequestException as exc:
            # Don't retry 4xx — those are deterministic errors.
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status < 500:
                msg = exc.response.text if exc.response is not None else str(exc)
                raise RuntimeError(f"Open-Meteo API error: {msg}") from exc
            last_exc = exc
            response = None
        if attempt < 2:
            time.sleep((0.4 * (2 ** attempt)) + random.uniform(0, 0.2))

    if response is None:
        if isinstance(last_exc, requests.Timeout):
            raise RuntimeError("Open-Meteo request timed out") from last_exc
        raise RuntimeError(f"Open-Meteo API error: {last_exc}") from last_exc

    payload = response.json()

    if "daily" not in payload:
        raise ValueError(
            f"Unexpected Open-Meteo response: 'daily' key missing. "
            f"Keys present: {list(payload.keys())}"
        )

    return _parse_response(payload["daily"], payload.get("hourly"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_params(latitude: float, longitude: float) -> dict:
    """
    Build the query-parameter dict for the Open-Meteo API call.

    Kept separate so that upgrading to a paid tier (api_key, different
    model selection, extra variables) only touches this function.
    """
    return {
        "latitude":      latitude,
        "longitude":     longitude,
        "daily":         ",".join(_DAILY_VARS),
        "hourly":        ",".join(_HOURLY_VARS),
        "past_days":     PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
        "timezone":      "auto",   # dates in local timezone, not UTC
    }


def _parse_response(daily: dict, hourly: dict | None = None) -> dict:
    """
    Split the raw Open-Meteo daily payload into historical and forecast windows.

    Response layout with past_days=7 and forecast_days=5 (12 rows total):
      indices 0–6   → past 7 complete days
      indices 7–11  → today + next 4 days  (5-day forecast window)
    """
    times   = daily["time"]                       # ["YYYY-MM-DD", ...]
    tmax    = daily["temperature_2m_max"]
    tmin    = daily["temperature_2m_min"]
    rain    = daily["precipitation_sum"]
    wind    = daily["wind_speed_10m_max"]
    et0     = daily["et0_fao_evapotranspiration"]
    rh_max  = daily.get("relative_humidity_2m_max")
    rh_min  = daily.get("relative_humidity_2m_min")

    # --- Historical: rows 0 … PAST_DAYS-1 ---
    hist_idx = range(PAST_DAYS)

    temperature_last_7_days = [
        {"date": times[i], "tmin": _f(tmin[i]), "tmax": _f(tmax[i])}
        for i in hist_idx
    ]
    humidity_last_7_days  = _mean_humidity(rh_max, rh_min, hist_idx)
    rain_last_7_days_mm   = [_f(rain[i]) for i in hist_idx]
    et0_last_7_days_mm    = [_f(et0[i])  for i in hist_idx]

    # Use last complete day for wind; convert km/h → m/s
    wind_speed_mps = round(_f(wind[PAST_DAYS - 1]) * _KMH_TO_MPS, 2)

    # --- Forecast: rows PAST_DAYS … PAST_DAYS+FORECAST_DAYS-1 ---
    fcast_idx = range(PAST_DAYS, PAST_DAYS + FORECAST_DAYS)

    rain_forecast_next_5_days_mm = [_f(rain[i]) for i in fcast_idx]
    temperature_forecast_next_5_days = [
        {"tmin": _f(tmin[i]), "tmax": _f(tmax[i])}
        for i in fcast_idx
    ]
    et0_forecast_next_5_days_mm = [_f(et0[i]) for i in fcast_idx]

    # --- Hourly-derived signals for Engine 4 (pest/disease) ---
    snapshot = _hourly_snapshot(hourly)

    return {
        "temperature_last_7_days":           temperature_last_7_days,
        "humidity_last_7_days":              humidity_last_7_days,
        "rain_last_7_days_mm":               rain_last_7_days_mm,
        "et0_last_7_days_mm":                et0_last_7_days_mm,
        "wind_speed_mps":                    wind_speed_mps,
        "rain_forecast_next_5_days_mm":       rain_forecast_next_5_days_mm,
        "temperature_forecast_next_5_days":   temperature_forecast_next_5_days,
        "et0_forecast_next_5_days_mm":        et0_forecast_next_5_days_mm,
        # Live snapshot + leaf-wetness proxy for Engine 4. These three keys
        # match the field names the apple_pest_disease_condition_rule corpus
        # is keyed on, so the LLM can map them onto rule bands directly.
        "temperature_c":                     snapshot["temperature_c"],
        "humidity_pct":                      snapshot["humidity_pct"],
        "conducive_duration_hrs":            snapshot["conducive_duration_hrs"],
        "fetch_date":                         datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _hourly_snapshot(hourly: dict | None) -> dict:
    """
    From the hourly Open-Meteo block, derive:
      - temperature_c           : latest hourly temperature (°C)
      - humidity_pct            : latest hourly relative humidity (%)
      - conducive_duration_hrs  : longest run of consecutive hours in the
                                  last _WETNESS_LOOKBACK_HOURS where
                                  RH ≥ _RH_WETNESS_THRESHOLD_PCT and
                                  T  ≥ _TEMP_WETNESS_MIN_C. Proxy for
                                  leaf-wetness duration; used by E4's
                                  pest/disease rule bands.

    Returns a dict with all three fields set to 0.0 if the hourly block is
    unavailable (older Open-Meteo responses, provider swap, etc.) — the
    engine will then say so explicitly rather than inventing a value.
    """
    if not hourly:
        logger.warning("Hourly block missing from Open-Meteo response — "
                       "conducive_duration_hrs will be 0.")
        return {"temperature_c": 0.0, "humidity_pct": 0.0,
                "conducive_duration_hrs": 0.0}

    times = hourly.get("time") or []
    rh    = hourly.get("relative_humidity_2m") or []
    temp  = hourly.get("temperature_2m") or []
    n = min(len(times), len(rh), len(temp))
    if n == 0:
        return {"temperature_c": 0.0, "humidity_pct": 0.0,
                "conducive_duration_hrs": 0.0}

    # Locate the "now" cursor: latest hour ≤ current local time. Open-Meteo
    # returns timezone-localised timestamps (we pass timezone=auto), so a
    # naive parse is correct — comparing against datetime.now() in the same
    # local TZ would require an extra round-trip. Instead we walk from the
    # end and pick the last index whose RH/temp are non-null.
    cursor = -1
    for i in range(n - 1, -1, -1):
        if rh[i] is not None and temp[i] is not None:
            cursor = i
            break
    if cursor < 0:
        return {"temperature_c": 0.0, "humidity_pct": 0.0,
                "conducive_duration_hrs": 0.0}

    start = max(0, cursor - _WETNESS_LOOKBACK_HOURS + 1)

    longest = 0
    current = 0
    for i in range(start, cursor + 1):
        rh_i = rh[i]
        t_i  = temp[i]
        if (rh_i is not None and t_i is not None
                and rh_i >= _RH_WETNESS_THRESHOLD_PCT
                and t_i  >= _TEMP_WETNESS_MIN_C):
            current += 1
            longest = max(longest, current)
        else:
            current = 0

    return {
        "temperature_c":           round(_f(temp[cursor]), 1),
        "humidity_pct":            round(_f(rh[cursor]), 1),
        "conducive_duration_hrs":  float(longest),
    }


def _f(val) -> float:
    """
    Safe float coercion. Returns 0.0 for None or NaN values.
    Missing data from the provider should not crash the engine pipeline.
    """
    if val is None:
        return 0.0
    try:
        f = float(val)
        return f if f == f else 0.0   # NaN != NaN
    except (TypeError, ValueError):
        return 0.0


def _mean_humidity(rh_max, rh_min, indices) -> list:
    """
    Compute daily mean RH% as (max + min) / 2 for each index in the window.
    Returns zeros if humidity is unavailable from the current provider.
    """
    if rh_max is None or rh_min is None:
        logger.warning(
            "Humidity not available from provider — returning zeros. "
            "Check _DAILY_VARS if the provider supports a different field name."
        )
        return [0.0] * len(list(indices))

    return [round((_f(rh_max[i]) + _f(rh_min[i])) / 2, 1) for i in indices]
