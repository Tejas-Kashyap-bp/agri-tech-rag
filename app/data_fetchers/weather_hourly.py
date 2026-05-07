"""
Direct Open-Meteo hourly fetch (this repo).

The sibling `Agri-integrated/data_fetchers/weather.py` returns only flat
snapshot fields (temperature_c, humidity_pct, conducive_duration_hrs) — its
`_hourly_snapshot` discards the raw hourly arrays. The apple-scab guardrail
needs hourly temperature_2m / relative_humidity_2m / dew_point_2m /
precipitation arrays so all four wet-detection rules (rainfall, RH≥90,
T-Td≤2, LWI≥0.7) can fire.

This module pulls the same Open-Meteo endpoint but keeps the hourly arrays.
Returns the dict that goes straight into `weather["hourly"]`. Failure-safe:
returns None on any error so the caller can fall back to the constant-shim.
"""

from __future__ import annotations

import logging
from typing import Optional

import urllib.parse
import urllib.request
import json as _json

log = logging.getLogger("data_fetchers.weather_hourly")

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_HOURLY_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "wind_speed_10m",
)
_PAST_DAYS = 2     # 48 h of history — matches the Mills lookback window
_FORECAST_DAYS = 2  # 48 h ahead so spray-window guardrails see forthcoming rain
_TIMEOUT_S = 8.0


def fetch_hourly(latitude: float, longitude: float) -> Optional[dict]:
    """Return a dict of hourly arrays for the last 48 h + next 48 h, or None.

    Shape:
        {
          "time":                 [...iso strings...],
          "temperature_2m":       [float, ...],
          "relative_humidity_2m": [float, ...],
          "dew_point_2m":         [float, ...],
          "precipitation":        [float, ...],
          "wind_speed_10m":       [float, ...],
        }
    """
    if latitude is None or longitude is None:
        return None
    try:
        qs = urllib.parse.urlencode({
            "latitude":      f"{float(latitude):.4f}",
            "longitude":     f"{float(longitude):.4f}",
            "hourly":        ",".join(_HOURLY_VARS),
            "past_days":     str(_PAST_DAYS),
            "forecast_days": str(_FORECAST_DAYS),
            "timezone":      "auto",
        })
        url = f"{_OPEN_METEO_URL}?{qs}"
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            return None
        # Trim to the indices where we have the union of required arrays so
        # downstream consumers don't see ragged edges.
        arrays = {k: hourly.get(k) or [] for k in ("time", *_HOURLY_VARS)}
        n = min(len(v) for v in arrays.values()) if all(arrays.values()) else 0
        if n == 0:
            return None
        return {k: v[:n] for k, v in arrays.items()}
    except Exception:
        log.warning("open_meteo_hourly_fetch_failed", exc_info=True)
        return None
