"""
Satellite Data Layer — Live (Sentinel Hub).

Replaces the old satellite_demo.py. Pulls real Sentinel-2 features for a
farm via the sibling agri-integrated fetcher, then shapes the four keys
that the advisory engines (E3 + E5) actually consume:

    {
        "ndvi_current":   float,
        "ndvi_delta_7d":  float,
        "ndre_current":   float,
        "evi_current":    float,
        "source":         "sentinel-hub",
    }

Polygon resolution
------------------
If `farm_polygon` is supplied (GeoJSON dict with type=Polygon, coordinates
nested as [[ [lon,lat], ... ]]), we use it directly. Otherwise we synthesize
a square bounding box centered on (latitude, longitude) whose side equals
sqrt(farm_area_m2). Sentinel-2 native resolution is 10 m, so a 1-acre farm
(~4047 m² → 64 m side) still yields ~6×6 = 36 averaged pixels per index.

Token caching
-------------
SH OAuth tokens last ~1 hour. We cache the token in-process and refresh it
~5 minutes before expiry so each /farm-advisory request does not pay the
~300-500 ms mint cost.
"""

import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, Optional

from app.config import settings  # ensures .env is loaded into os.environ

_INTEGRATED_PATH = settings.AGRI_INTEGRATED_PATH
if _INTEGRATED_PATH and _INTEGRATED_PATH not in sys.path:
    sys.path.insert(0, _INTEGRATED_PATH)

from data_fetchers.satellite import (  # type: ignore  # noqa: E402
    get_satellite_features,
    get_token,
)


log = logging.getLogger("data_fetchers.satellite_live")


# ---------------------------------------------------------------------------
# Lookback cap
# ---------------------------------------------------------------------------
# Audit (see plan): every downstream consumer reads only ndvi_current,
# ndvi_delta_7d, ndre_current, evi_current. The only signal needing history
# is ndvi_delta_7d which compares the latest reading against an observation
# ≥7 days back. Sentinel-2 revisits every 5 days with ±2-day window search,
# so 45 days yields ~9 candidate observations — robust against 40–50% cloud
# rejection typical in HP/Kashmir apple regions, especially during monsoon.
# Walking from sowing_date (months/years back) is wasted work that costs
# SH-trial quota and ~120-180s of wall-clock per call.

LOOKBACK_DAYS: int = 45


def _effective_start(sowing_date: _date) -> _date:
    """Cap the SH walk start to today − LOOKBACK_DAYS. For crops sown more
    recently than the cap, fall through to sowing_date (so seedling logic
    is unaffected)."""
    today = _date.today()
    return max(sowing_date, today - timedelta(days=LOOKBACK_DAYS))


# ---------------------------------------------------------------------------
# Per-farm-per-day disk cache
# ---------------------------------------------------------------------------
# Sentinel-2 revisit period is 5 days, so refreshing satellite features
# more than once a day cannot produce fresher data. A simple JSON-on-disk
# cache keyed by (cache_key, today) lets repeat /farm-advisory calls for
# the same farm skip the SH walk entirely. TTL 24 h. Cache failures must
# never break the request — every helper swallows exceptions.

_CACHE_DIR = Path(__file__).resolve().parents[1] / "storage" / "satellite_cache"
_CACHE_TTL_HOURS: float = 24.0
_CACHE_PRUNE_DAYS: int = 7


def _cache_key(
    farm_polygon: Optional[dict],
    latitude: Optional[float],
    longitude: Optional[float],
    farm_area_acres: Optional[float],
) -> str:
    if isinstance(farm_polygon, dict) and farm_polygon.get("coordinates"):
        seed = json.dumps(farm_polygon["coordinates"], sort_keys=True)
    else:
        # round to 4 dp ≈ 11 m on the ground — finer than Sentinel-2 pixels
        lat = round(float(latitude), 4) if latitude is not None else None
        lon = round(float(longitude), 4) if longitude is not None else None
        seed = f"{lat},{lon},{farm_area_acres or 1.0}"
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}_{_date.today().isoformat()}.json"


def _cache_get(key: str) -> Optional[dict]:
    fp = _cache_path(key)
    if not fp.exists():
        return None
    try:
        age_h = (time.time() - fp.stat().st_mtime) / 3600.0
        if age_h > _CACHE_TTL_HOURS:
            return None
        return json.loads(fp.read_text())
    except Exception:
        return None


def _cache_put(key: str, payload: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fp = _cache_path(key)
        fd, tmp = tempfile.mkstemp(prefix=".sat_", dir=str(_CACHE_DIR))
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload))
        os.replace(tmp, fp)
    except Exception:
        log.warning("satellite_cache_put_failed", exc_info=True)


def _cache_prune() -> None:
    """Best-effort cleanup of cache files older than _CACHE_PRUNE_DAYS.
    Called lazily on cache miss so the directory cannot grow unbounded."""
    try:
        if not _CACHE_DIR.exists():
            return
        cutoff = time.time() - _CACHE_PRUNE_DAYS * 86400
        for fp in _CACHE_DIR.glob("*.json"):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0}
_REFRESH_BUFFER_S = 300  # refresh 5 min before actual expiry


def _get_cached_token() -> str:
    """Mint and cache a Sentinel Hub OAuth token. Refreshes ~5 min before
    expiry so concurrent requests share one token."""
    cid = os.environ.get("SENTINEL_CLIENT_ID")
    csec = os.environ.get("SENTINEL_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError(
            "SENTINEL_CLIENT_ID / SENTINEL_CLIENT_SECRET missing — set them in .env"
        )

    with _TOKEN_LOCK:
        now = time.monotonic()
        if (
            _TOKEN_CACHE["token"]
            and now < _TOKEN_CACHE["expires_at"] - _REFRESH_BUFFER_S
        ):
            return _TOKEN_CACHE["token"]
        token = get_token(cid, csec)
        # SH tokens are valid for 3600 s. Conservatively assume 3600 even if
        # the upstream lib does not return expires_in.
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = now + 3600
        return token


# ---------------------------------------------------------------------------
# Polygon resolution
# ---------------------------------------------------------------------------

# Standard area conversions. 1 acre = 4046.8564224 m².
_ACRE_M2 = 4046.8564224
# Earth radius approximations for converting metres → degrees.
# 1 degree of latitude ≈ 111_320 m (constant). Longitude varies by cos(lat).
_M_PER_DEG_LAT = 111_320.0


def _bbox_polygon(latitude: float, longitude: float, side_m: float) -> dict:
    """Build a GeoJSON Polygon centered on (latitude, longitude) with the
    given square side length in metres. Used when the farm has no stored
    farm_polygon — clamps to a sane minimum so noise from a single missing
    pixel does not zero the mean."""
    side_m = max(side_m, 30.0)  # 30 m floor → still ~3×3 Sentinel-2 pixels
    half = side_m / 2.0
    dlat = half / _M_PER_DEG_LAT
    dlon = half / (_M_PER_DEG_LAT * max(math.cos(math.radians(latitude)), 1e-3))
    return {
        "type": "Polygon",
        "coordinates": [[
            [longitude - dlon, latitude - dlat],
            [longitude + dlon, latitude - dlat],
            [longitude + dlon, latitude + dlat],
            [longitude - dlon, latitude + dlat],
            [longitude - dlon, latitude - dlat],
        ]],
    }


def _resolve_polygon(
    farm_polygon: Optional[dict],
    latitude: Optional[float],
    longitude: Optional[float],
    farm_area_m2: Optional[float],
    farm_area_acres: Optional[float],
) -> dict:
    """Prefer the stored farm_polygon. Fall back to a square bbox synthesized
    from lat/lon and farm area. Raises if no usable inputs are present."""
    if isinstance(farm_polygon, dict) and farm_polygon.get("coordinates"):
        return farm_polygon
    if latitude is None or longitude is None:
        raise RuntimeError(
            "Cannot resolve farm geometry — farm_polygon missing and no lat/lon available"
        )
    area_m2 = (
        farm_area_m2
        if farm_area_m2 is not None
        else (farm_area_acres or 1.0) * _ACRE_M2
    )
    side_m = math.sqrt(max(area_m2, _ACRE_M2 * 0.1))  # >= 0.1 acre
    return _bbox_polygon(float(latitude), float(longitude), side_m)


# ---------------------------------------------------------------------------
# Public API — drop-in replacement for the old satellite_demo.get_satellite_data
# ---------------------------------------------------------------------------

def get_satellite_data(
    *,
    sowing_date: Any,
    farm_polygon: Optional[dict] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    farm_area_m2: Optional[float] = None,
    farm_area_acres: Optional[float] = None,
) -> dict[str, Any]:
    """
    Fetch live NDVI / NDRE / EVI / NDVI-delta for a farm.

    Returns the same five keys that engines E3 (`satellite_layer.classify`)
    and E5 (`yield_layer.classify_yield_satellite`) consume, so swapping in
    this adapter requires no changes inside the engines themselves.

    Args:
        sowing_date:      `date` or "YYYY-MM-DD" string. Season-start anchor.
        farm_polygon:     GeoJSON Polygon dict from Supabase, optional.
        latitude:         Required if farm_polygon is missing.
        longitude:        Required if farm_polygon is missing.
        farm_area_m2:     Used to size the synthesized polygon, optional.
        farm_area_acres:  Used if farm_area_m2 absent. Defaults to 1 acre.

    Raises:
        RuntimeError: on missing credentials or unrecoverable Sentinel Hub
                      / geometry resolution failures.
    """
    if isinstance(sowing_date, _date):
        sowing_dt = sowing_date
    else:
        sowing_dt = _date.fromisoformat(str(sowing_date))

    # Per-farm-per-day cache check — Sentinel-2 revisits every 5 days, so
    # repeat /farm-advisory calls within 24 h cannot get fresher data.
    key = _cache_key(farm_polygon, latitude, longitude, farm_area_acres)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    _cache_prune()

    polygon = _resolve_polygon(
        farm_polygon, latitude, longitude, farm_area_m2, farm_area_acres
    )

    # Cap the SH walk to LOOKBACK_DAYS — see _effective_start.
    sowing_iso = _effective_start(sowing_dt).isoformat()

    token = _get_cached_token()
    feats = get_satellite_features(token, polygon, sowing_iso)

    payload = {
        "ndvi_current":  feats.get("ndvi_current"),
        "ndvi_delta_7d": feats.get("ndvi_delta_7d") or 0.0,
        "ndre_current":  feats.get("ndre_current"),
        "evi_current":   feats.get("evi_current"),
        "source":        "sentinel-hub",
    }
    _cache_put(key, payload)
    return payload
