"""
get_soil_data.py — Single entrypoint for the soil data pipeline.

Priority: configurable (default: farmer → soilgrids → supabase)
Each layer only fills keys that are STILL MISSING from previous layers.

Completeness guarantee:
  - Supabase is always attempted if any keys remain missing after the
    priority loop, regardless of the requested priority_order.
  - Any key still missing after all sources gets a null sentinel:
    {"value": None, "source": "unavailable", "confidence": 0.0}

This means to_dict() always contains all 12 nutrient keys — never absent.
"""

import json
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from .models import CONFIDENCE_SCORES, NUTRIENT_KEYS, SoilData
from .layers.ocr_layer import extract_from_report
from .layers.soilgrids_layer import fetch_from_soilgrids
from .layers.master_table_layer import fetch_from_master

logger = logging.getLogger(__name__)

# ── Default priority ──────────────────────────────────────────────────────────
# Can be overridden at call time via the priority_order parameter.
# config.json can also override the default for the entire process.
DEFAULT_PRIORITY: List[str] = ["farmer", "soilgrids", "supabase"]

_cfg_path = Path(__file__).parent / "config.json"
if _cfg_path.exists():
    try:
        with open(_cfg_path) as _f:
            _cfg = json.load(_f)
        DEFAULT_PRIORITY = _cfg.get("priority_order", DEFAULT_PRIORITY)
    except (json.JSONDecodeError, OSError) as _exc:
        logger.warning("soil config.json malformed (%s); using built-in default priority", _exc)


# ── Layer wrappers ────────────────────────────────────────────────────────────
# Each wrapper receives the current SoilData and a context dict.
# It checks preconditions, runs the layer, and returns the list of keys filled.

def _run_ocr_layer(result: SoilData, ctx: dict) -> list:
    """Run OCR on a farmer soil report (PDF/image)."""
    report_path  = ctx.get("report_path")
    report_bytes = ctx.get("report_bytes")
    if not report_path and not report_bytes:
        logger.info("[Pipeline] Farmer layer skipped (no report provided)")
        return []
    still_missing = _all_missing(result)
    if not still_missing:
        return []
    logger.info("[Pipeline] Farmer layer: running OCR")
    ocr_data = extract_from_report(report_path=report_path, report_bytes=report_bytes)
    return _merge(result, ocr_data)


def _run_soilgrids_layer(result: SoilData, ctx: dict) -> list:
    """Fetch from ISRIC SoilGrids using lat/lon coordinates."""
    lat = ctx.get("lat")
    lon = ctx.get("lon")
    if lat is None or lon is None:
        logger.info("[Pipeline] SoilGrids layer skipped (no lat/lon provided)")
        return []
    still_missing = _all_missing(result)
    if not still_missing:
        return []
    logger.info(f"[Pipeline] SoilGrids layer: fetching {still_missing}")
    sg_data = fetch_from_soilgrids(lat=lat, lon=lon, missing_keys=still_missing)
    return _merge(result, sg_data)


def _run_master_layer(result: SoilData, ctx: dict) -> list:
    """Fetch regional averages from the Supabase soil_master table."""
    still_missing = _all_missing(result)
    if not still_missing:
        return []
    logger.info(f"[Pipeline] Supabase layer: fetching {still_missing}")
    mt_data = fetch_from_master(
        state     = ctx["state"],
        district  = ctx.get("district"),
        soil_type = ctx["soil_type"],
        crop      = ctx["crop"],
        missing_keys = still_missing,
    )
    return _merge(result, mt_data)


# Registry maps source name → layer wrapper function
LAYER_REGISTRY: Dict[str, Callable] = {
    "farmer":    _run_ocr_layer,
    "soilgrids": _run_soilgrids_layer,
    "supabase":  _run_master_layer,
}


# ── Public entrypoint ─────────────────────────────────────────────────────────

def get_soil_data(
    report_path:    Optional[Union[str, Path]] = None,
    report_bytes:   Optional[bytes]            = None,
    lat:            Optional[float]            = None,
    lon:            Optional[float]            = None,
    state:          str                        = "Haryana",
    district:       Optional[str]              = None,
    soil_type:      str                        = "Loam",
    crop:           str                        = "Maize",
    priority_order: Optional[List[str]]        = None,
) -> SoilData:
    """
    Run the soil data pipeline and return a fully resolved SoilData.

    All 12 nutrient keys are always present in the result. Keys that no
    source could fill are set to None with confidence=0.0 (sentinel).

    Args:
        report_path:    Path to farmer soil report (PDF/JPG/PNG).
        report_bytes:   Raw image bytes (alternative to report_path).
        lat, lon:       Decimal degrees (WGS84) for SoilGrids lookup.
        state:          State name for Supabase lookup.
        district:       District name for Supabase lookup (fuzzy-matched).
        soil_type:      Soil texture class (e.g. "Loam", "Sandy").
        crop:           Crop name (e.g. "Maize").
        priority_order: Source execution order. Defaults to DEFAULT_PRIORITY.
                        Priority = preference; completeness = requirement.
                        Supabase is always attempted as a final fallback.

    Returns:
        SoilData with overall_confidence and per-parameter source tracking.
    """
    if priority_order is None:
        priority_order = DEFAULT_PRIORITY

    result = SoilData(state=state, district=district, soil_type=soil_type, crop=crop)

    # Context passed to every layer wrapper
    ctx = {
        "report_path":  report_path,
        "report_bytes": report_bytes,
        "lat":          lat,
        "lon":          lon,
        "state":        state,
        "district":     district,
        "soil_type":    soil_type,
        "crop":         crop,
    }

    sources_attempted: set = set()

    # ── Priority loop ─────────────────────────────────────────────────────────
    for source_name in priority_order:
        if not _all_missing(result):
            logger.info("[Pipeline] All keys filled — stopping early")
            break

        layer_fn = LAYER_REGISTRY.get(source_name)
        if not layer_fn:
            logger.warning(f"[Pipeline] Unknown source '{source_name}' in priority_order — skipping")
            continue

        merged = layer_fn(result, ctx)
        sources_attempted.add(source_name)

        if merged:
            logger.info(f"[Pipeline] '{source_name}' filled: {merged}")

        logger.info(
            "[Pipeline] After '%s': %d/%d keys filled, still missing: %s",
            source_name, len(result.nutrients), len(NUTRIENT_KEYS), _all_missing(result),
        )

    # ── Completeness guarantee ────────────────────────────────────────────────
    # If any keys are still missing and Supabase hasn't been tried yet, force it.
    # This fires when priority_order excludes "supabase" (e.g. ["farmer"] only).
    if _all_missing(result) and "supabase" not in sources_attempted:
        logger.warning(
            "[Pipeline] Completeness guarantee: forcing Supabase for missing keys: %s",
            _all_missing(result),
        )
        _run_master_layer(result, ctx)
        sources_attempted.add("supabase")

    # ── Sentinel fill ─────────────────────────────────────────────────────────
    # Keys still missing after all sources = truly unavailable.
    # Set a null sentinel so to_dict() always contains every key.
    for key in _all_missing(result):
        logger.warning("[Pipeline] '%s' unavailable from all sources — setting null sentinel", key)
        result.set(key, None, source="unavailable", confidence=CONFIDENCE_SCORES["unavailable"])

    # ── Overall confidence ────────────────────────────────────────────────────
    if result.confidences:
        result.overall_confidence = round(
            sum(result.confidences.values()) / len(result.confidences), 4
        )

    logger.info("[Pipeline] Done → %s", result)
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_missing(data: SoilData) -> list:
    """All NUTRIENT_KEYS not yet set in the SoilData (absent from nutrients dict)."""
    return [k for k in NUTRIENT_KEYS if k not in data.nutrients]


def _merge(base: SoilData, incoming: SoilData) -> list:
    """
    Copy keys from incoming → base ONLY if not already present.
    Higher-priority layer values are never overwritten.

    Returns list of keys actually written.
    """
    written = []
    for key, value in incoming.nutrients.items():
        if key not in base.nutrients:
            base.set(
                key,   value,
                source      = incoming.sources.get(key, "unknown"),
                confidence  = incoming.confidences.get(key, 0.0),
                unit        = incoming.units.get(key),
                ideal_range = incoming.ideal_ranges.get(key),
                status      = incoming.statuses.get(key),
                method      = incoming.methods.get(key),
            )
            written.append(key)
    return written
