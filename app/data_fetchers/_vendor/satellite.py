# ---------------------------------------------------------------------------
# data_fetchers/satellite.py
#
# Sentinel-2 satellite data fetcher.
# Collects NDVI and NDWI for a farm polygon across the full crop season
# (sowing_date → today) and returns only the fields consumed by the engines.
#
# Output contract:
#   ndvi_timeseries    list[{"date": str, "value": float}]  Engine 1
#   ndvi_current       float                                Engine 3 (INM)
#   ndvi_trend         "rising"|"falling"|"plateau"|"unknown"  Engine 1 / 2 / 4
#   ndwi_current       float                                Engine 2
#   ndwi_previous      float | None                         Engine 2 (delta)
#   observation_count  int                                  quality signal
#   season_start       str  YYYY-MM-DD                      metadata
#   season_end         str  YYYY-MM-DD                      metadata
#
# Collection strategy:
#   - Walk forward from sowing_date to today in STEP_DAYS (5-day) increments.
#   - For each step, search a ±WINDOW_DAYS (2-day) window for valid imagery.
#   - Tile-level cloud filter: maxCloudCoverage = 20%.
#   - Skip any window where the farm polygon returns NaN (cloud over the field).
#   - No cap on observations — collect every valid point in the season.
# ---------------------------------------------------------------------------

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import rasterio.io
import requests
from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOKEN_URL   = "https://services.sentinel-hub.com/auth/realms/main/protocol/openid-connect/token"
_PROCESS_URL = "https://services.sentinel-hub.com/api/v1/process"

MAX_CLOUD_COVERAGE = 20   # % — tile-level (100 km × 100 km) cloud threshold
STEP_DAYS          = 5    # Sentinel-2 revisit cycle in days
WINDOW_DAYS        = 2    # ± days around each step to catch the closest image
TREND_DEADBAND     = 0.01 # avg NDVI slope threshold; within ±this = plateau
REQUEST_TIMEOUT    = 30   # seconds per API call

# Evalscript runs on Sentinel Hub's server — returns a 4-band GeoTIFF.
# Band 1: NDVI = (B08 - B04) / (B08 + B04)         — canopy greenness
# Band 2: NDWI = (B08 - B11) / (B08 + B11)         — crop water stress (B11 20m, resampled by SH)
# Band 3: NDRE = (B08 - B05) / (B08 + B05)         — red-edge / N + chlorophyll status (E3)
# Band 4: EVI  = 2.5*(B08-B04)/(B08+6*B04-7.5*B02+1) — enhanced vegetation index (E5)
_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B02", "B04", "B05", "B08", "B11"],
    output: { bands: 4, sampleType: "FLOAT32" }
  }
}
function evaluatePixel(s) {
  let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04)
  let ndwi = (s.B08 - s.B11) / (s.B08 + s.B11)
  let ndre = (s.B08 - s.B05) / (s.B08 + s.B05)
  let evi  = 2.5 * (s.B08 - s.B04) / (s.B08 + 6.0 * s.B04 - 7.5 * s.B02 + 1.0)
  return [ndvi, ndwi, ndre, evi]
}
"""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_token(client_id: str, client_secret: str) -> str:
    """
    Fetch a Sentinel Hub OAuth2 access token.

    Args:
        client_id:     Sentinel Hub OAuth client ID.
        client_secret: Sentinel Hub OAuth client secret.

    Returns:
        Access token string.

    Raises:
        ValueError:   if credentials are empty.
        RuntimeError: if the token request fails.
    """
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")

    client = BackendApplicationClient(client_id=client_id)
    oauth  = OAuth2Session(client=client)

    try:
        token = oauth.fetch_token(
            token_url=_TOKEN_URL,
            client_secret=client_secret,
            include_client_id=True,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        raise RuntimeError(f"Sentinel Hub token fetch failed: {exc}") from exc

    return token["access_token"]


# ---------------------------------------------------------------------------
# Internal: single window fetch
# ---------------------------------------------------------------------------

def _fetch_window(
    access_token: str,
    polygon: dict,
    start_date: str,
    end_date: str,
) -> dict | None:
    """
    Fetch mean NDVI and NDWI for a farm polygon over one time window.

    Args:
        access_token: Valid Sentinel Hub bearer token.
        polygon:      GeoJSON Polygon dict — {"type": "Polygon", "coordinates": [...]}.
        start_date:   ISO string "YYYY-MM-DDTHH:MM:SSZ".
        end_date:     ISO string "YYYY-MM-DDTHH:MM:SSZ".

    Returns:
        {"ndvi": float, "ndwi": float} on success,
        None if the farm polygon is under cloud (NaN result).

    Raises:
        RuntimeError: on HTTP or network errors.
    """
    coords = polygon["coordinates"][0]
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]

    closed_polygon = {"type": "Polygon", "coordinates": [coords]}

    payload = {
        "input": {
            "bounds": {"geometry": closed_polygon},
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "maxCloudCoverage": MAX_CLOUD_COVERAGE,
                    "timeRange": {"from": start_date, "to": end_date},
                },
            }],
        },
        "output": {
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]
        },
        "evalscript": _EVALSCRIPT,
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            _PROCESS_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise RuntimeError("Sentinel Hub request timed out") from exc
    except requests.RequestException as exc:
        msg = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"Sentinel Hub API error: {msg}") from exc

    with rasterio.io.MemoryFile(response.content) as memfile:
        with memfile.open() as src:
            ndvi_array = src.read(1)
            ndwi_array = src.read(2)
            ndre_array = src.read(3)
            evi_array  = src.read(4)

    ndvi_mean = float(np.nanmean(ndvi_array))
    ndwi_mean = float(np.nanmean(ndwi_array))
    ndre_mean = float(np.nanmean(ndre_array))
    evi_mean  = float(np.nanmean(evi_array))

    # NaN means no valid pixels — cloud fully covered the farm polygon
    if np.isnan(ndvi_mean):
        return None

    return {
        "ndvi": ndvi_mean,
        "ndwi": ndwi_mean,
        "ndre": ndre_mean,
        "evi":  evi_mean,
    }


# ---------------------------------------------------------------------------
# Internal: trend computation
# ---------------------------------------------------------------------------

def _compute_trend(timeseries: list) -> str:
    """
    Classify the direction of a sorted NDVI timeseries.

    Computes the average slope across all consecutive pairs.
    Returns "unknown" if fewer than 2 observations exist.

    Args:
        timeseries: list of {"date": str, "value": float}, sorted ascending by date.

    Returns:
        "rising" | "falling" | "plateau" | "unknown"
    """
    if len(timeseries) < 2:
        return "unknown"

    slopes = [
        timeseries[i]["value"] - timeseries[i - 1]["value"]
        for i in range(1, len(timeseries))
    ]

    avg_slope = sum(slopes) / len(slopes)

    if avg_slope > TREND_DEADBAND:
        return "rising"
    if avg_slope < -TREND_DEADBAND:
        return "falling"
    return "plateau"


# ---------------------------------------------------------------------------
# Public: main function
# ---------------------------------------------------------------------------

def get_satellite_features(
    access_token: str,
    polygon: dict,
    sowing_date: str,
) -> dict:
    """
    Collect full-season satellite features for a farm polygon.

    Walks forward from sowing_date to today in 5-day steps (Sentinel-2
    revisit cycle). For each step, searches a ±2 day window for valid
    imagery. Skips windows where the farm polygon is under cloud (NaN).
    Collects every valid observation — no hard cap.

    Args:
        access_token: Valid Sentinel Hub bearer token (from get_token()).
        polygon:      GeoJSON Polygon dict — the farm boundary.
        sowing_date:  "YYYY-MM-DD" — the start of the crop cycle for this farm.

    Returns:
        {
            "ndvi_timeseries":   list[{"date": str, "value": float}],
            "ndre_timeseries":   list[{"date": str, "value": float}],
            "evi_timeseries":    list[{"date": str, "value": float}],
            "ndwi_timeseries":   list[{"date": str, "value": float}],
            "ndvi_current":      float | None,
            "ndre_current":      float | None,
            "evi_current":       float | None,
            "ndvi_delta_7d":     float | None,   # latest − value ~7 days back
            "ndvi_trend":        "rising" | "falling" | "plateau" | "unknown",
            "ndwi_current":      float | None,
            "ndwi_previous":     float | None,
            "observation_count": int,
            "season_start":      str,   # = sowing_date
            "season_end":        str,   # = today
        }

    Raises:
        ValueError:   if sowing_date is in the future.
        RuntimeError: on unrecoverable Sentinel Hub API failures.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    start = datetime.strptime(sowing_date, "%Y-%m-%d")

    if start > today:
        raise ValueError(f"sowing_date {sowing_date} is in the future")

    ndvi_timeseries: list = []
    ndre_timeseries: list = []
    evi_timeseries:  list = []
    ndwi_timeseries: list = []
    ndwi_observations: list = []  # parallel to ndvi_timeseries — kept for ndwi_previous

    current = start

    while current <= today:
        window_start = current - timedelta(days=WINDOW_DAYS)
        window_end   = min(current + timedelta(days=WINDOW_DAYS), today)

        try:
            result = _fetch_window(
                access_token,
                polygon,
                window_start.strftime("%Y-%m-%dT00:00:00Z"),
                window_end.strftime("%Y-%m-%dT23:59:59Z"),
            )
        except RuntimeError as exc:
            logger.warning("Skipping %s — API error: %s", current.date(), exc)
            result = None

        if result is not None:
            date_str = current.strftime("%Y-%m-%d")
            ndvi_timeseries.append({"date": date_str, "value": result["ndvi"]})
            ndre_timeseries.append({"date": date_str, "value": result["ndre"]})
            evi_timeseries.append({"date":  date_str, "value": result["evi"]})
            ndwi_timeseries.append({"date": date_str, "value": result["ndwi"]})
            ndwi_observations.append(result["ndwi"])

        current += timedelta(days=STEP_DAYS)

    observation_count = len(ndvi_timeseries)

    # ndvi_delta_7d: latest value minus the value sampled ~7 days back. Walk
    # the series from newest backward and pick the first observation whose
    # date is at least 7 days before the latest. Returns None if the series
    # does not span 7 days yet.
    ndvi_delta_7d = None
    if observation_count >= 2:
        latest_iso = ndvi_timeseries[-1]["date"]
        latest_dt  = datetime.strptime(latest_iso, "%Y-%m-%d")
        latest_val = ndvi_timeseries[-1]["value"]
        for obs in reversed(ndvi_timeseries[:-1]):
            obs_dt = datetime.strptime(obs["date"], "%Y-%m-%d")
            if (latest_dt - obs_dt).days >= 7:
                ndvi_delta_7d = latest_val - obs["value"]
                break

    return {
        "ndvi_timeseries":   ndvi_timeseries,
        "ndre_timeseries":   ndre_timeseries,
        "evi_timeseries":    evi_timeseries,
        "ndwi_timeseries":   ndwi_timeseries,
        "ndvi_current":      ndvi_timeseries[-1]["value"] if observation_count >= 1 else None,
        "ndre_current":      ndre_timeseries[-1]["value"] if observation_count >= 1 else None,
        "evi_current":       evi_timeseries[-1]["value"]  if observation_count >= 1 else None,
        "ndvi_delta_7d":     ndvi_delta_7d,
        "ndvi_trend":        _compute_trend(ndvi_timeseries),
        "ndwi_current":      ndwi_observations[-1] if observation_count >= 1 else None,
        "ndwi_previous":     ndwi_observations[-2] if observation_count >= 2 else None,
        "observation_count": observation_count,
        "season_start":      sowing_date,
        "season_end":        today.strftime("%Y-%m-%d"),
    }
