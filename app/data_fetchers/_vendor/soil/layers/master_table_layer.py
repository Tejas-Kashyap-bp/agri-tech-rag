"""
layers/master_table_layer.py — Layer 3: Supabase soil_master lookup.

District resolution strategy (before any DB query):
  1. Alias mapping  — known alternate names (e.g. Gurgaon → Gurugram)
  2. Fuzzy match    — rapidfuzz WRatio ≥ 85 against known districts for the state
  3. Original name  — passed as-is; DB fallback chain handles misses

DB query fallback chain (mirrors coverage_level in the schema):
  1. Exact match:    state + resolved_district + soil_type + crop  (coverage_level=1)
  2. State fallback: state + soil_type + crop, district IS NULL    (coverage_level=2)
  3. Relaxed:        state + crop only (ignore soil_type)

Range handling:
  Supabase stores value_range as "80–160". We always compute the mean.
  Falls back to typical_value if value_range is absent or unparseable.

Schema reference (soil_master):
    nutrient_key    VARCHAR  — e.g. 'nitrogen_N', 'ec_EC'
    typical_value   NUMERIC  — pre-computed mean (backward-compat)
    value_range     VARCHAR  — e.g. '80–160'  (add this column to Supabase)
    unit            VARCHAR  — 'kg/ha', 'ppm', '%', 'dS/m', etc.
    ideal_range     VARCHAR  — '6.0-7.5'
    typical_status  VARCHAR  — 'Low' | 'Normal' | 'High'
    coverage_level  SMALLINT — 1 (district) | 2 (state fallback)
"""

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from supabase import create_client, Client

from ..models import CONFIDENCE_SCORES, SoilData

# Env loading is centralized in api/main.py. The previous unbounded upward
# walk used to pick up ~/.env on developer laptops.

logger = logging.getLogger(__name__)

# Path to the district alias file (sibling of this package)
_ALIAS_FILE = Path(__file__).resolve().parents[1] / "district_aliases.json"

# Graceful degradation if rapidfuzz is not installed
try:
    from rapidfuzz import fuzz as _fuzz, process as _process
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False
    logger.warning("[MasterTable] rapidfuzz not installed — fuzzy district matching disabled. "
                   "Run: pip install rapidfuzz")


# ── Supabase client singleton ─────────────────────────────────────────────────

_supabase: Optional[Client] = None


def _get_client() -> Client:
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        # Accept either key name (root .env uses SERVICE_ROLE_KEY, soil .env uses KEY)
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) must be set in .env"
            )
        _supabase = create_client(url, key)
        logger.info("[MasterTable] Supabase client initialised (url=%s).", url)
    return _supabase


# ── Public function ───────────────────────────────────────────────────────────

def fetch_from_master(
    state:        str,
    district:     Optional[str],
    soil_type:    str,
    crop:         str,
    missing_keys: list,
) -> SoilData:
    """
    Look up missing_keys from the soil_master Supabase table.
    Resolves district via alias/fuzzy matching before querying.

    Returns SoilData with source="supabase" and found fields populated.
    """
    data = SoilData()

    if not missing_keys:
        return data

    # Resolve district name before hitting the DB
    resolved_district = _resolve_district(state, district)

    try:
        rows = _fetch_rows(state, resolved_district, soil_type, crop)
        _parse_into(rows, missing_keys, data)
        logger.info(
            "[MasterTable] Resolved %d fields from %d rows for %s/%s/%s/%s",
            len(data.nutrients), len(rows), state, resolved_district, soil_type, crop,
        )
    except EnvironmentError as e:
        logger.error("[MasterTable] Config error: %s", e)
    except Exception as exc:
        logger.warning("[MasterTable] Query failed: %s", exc, exc_info=True)

    return data


# ── District resolution ───────────────────────────────────────────────────────

def _resolve_district(state: str, district: Optional[str]) -> Optional[str]:
    """
    Resolve a district name to its canonical form for DB lookup.

    Tier 1: Alias mapping (e.g. "gurgaon" → "Gurugram")
    Tier 2: Fuzzy matching against known districts for the state (≥ 85 score)
    Tier 3: Return original string — DB fallback chain handles misses gracefully
    """
    if not district:
        return None

    # Tier 1: alias lookup (case-insensitive key)
    aliases = _load_district_aliases()
    normalized = district.strip().lower()
    if normalized in aliases:
        resolved = aliases[normalized]
        if resolved.lower() != normalized:
            logger.info("[MasterTable] Alias match: '%s' → '%s'", district, resolved)
        return resolved

    # Tier 2: fuzzy match (requires rapidfuzz)
    if _RAPIDFUZZ_AVAILABLE:
        candidates = _get_state_districts(state)
        if candidates:
            match, score, _ = _process.extractOne(district, candidates, scorer=_fuzz.WRatio)
            if score >= 85:
                if match != district:
                    logger.info(
                        "[MasterTable] Fuzzy match: '%s' → '%s' (score=%d)", district, match, score
                    )
                return match
            logger.debug(
                "[MasterTable] Fuzzy threshold not met for '%s' (best='%s', score=%d)",
                district, match, score,
            )

    # Tier 3: pass original through; DB fallback handles it
    return district


@lru_cache(maxsize=1)
def _load_district_aliases() -> dict:
    """Load district alias mapping (cached on first call)."""
    try:
        with open(_ALIAS_FILE) as f:
            return {k.lower(): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        logger.warning("[MasterTable] district_aliases.json not found — alias matching disabled")
        return {}


@lru_cache(maxsize=32)
def _get_state_districts(state: str) -> tuple:
    """
    Fetch distinct district names for a state from Supabase (cached per state).
    Returns a tuple so the result is hashable for lru_cache.
    """
    try:
        rows = (
            _get_client()
            .table("soil_master")
            .select("district")
            .eq("state", state)
            .not_.is_("district", "null")
            .execute()
        ).data
        return tuple(sorted(set(r["district"] for r in rows if r.get("district"))))
    except Exception as exc:
        logger.warning("[MasterTable] Could not fetch district list for %s: %s", state, exc)
        return tuple()


# ── DB query with fallback chain ──────────────────────────────────────────────

@lru_cache(maxsize=64)
def _fetch_rows(state: str, district: Optional[str], soil_type: str, crop: str) -> tuple:
    """
    Cached query — returns a tuple of row dicts (hashable for lru_cache).

    Fallback chain:
      1. District-level exact match  (coverage_level=1)
      2. State-level fallback        (coverage_level=2, district IS NULL)
      3. Relaxed: state + crop only  (ignores soil_type)
    """
    client = _get_client()

    # Attempt 1: district-level exact match
    if district:
        rows = (
            client.table("soil_master")
            .select("nutrient_key, typical_value, value_range, unit, ideal_range, typical_status, coverage_level")
            .eq("state",          state)
            .eq("district",       district)
            .eq("soil_type",      soil_type)
            .eq("crop",           crop)
            .eq("coverage_level", 1)
            .execute()
        ).data
        if rows:
            logger.debug("[MasterTable] District match: %d rows", len(rows))
            return tuple(rows)

    # Attempt 2: state-level fallback (district IS NULL)
    rows = (
        client.table("soil_master")
        .select("nutrient_key, typical_value, value_range, unit, ideal_range, typical_status, coverage_level")
        .eq("state",          state)
        .is_("district",      "null")
        .eq("soil_type",      soil_type)
        .eq("crop",           crop)
        .eq("coverage_level", 2)
        .execute()
    ).data
    if rows:
        logger.debug("[MasterTable] State-level match: %d rows", len(rows))
        return tuple(rows)

    # Attempt 3: relaxed — state + crop only (ignore soil_type)
    rows = (
        client.table("soil_master")
        .select("nutrient_key, typical_value, value_range, unit, ideal_range, typical_status, coverage_level")
        .eq("state", state)
        .eq("crop",  crop)
        .order("coverage_level")   # prefer district rows over state rows
        .execute()
    ).data
    if rows:
        logger.debug("[MasterTable] Relaxed match (no soil_type filter): %d rows", len(rows))
        return tuple(rows)

    logger.warning("[MasterTable] No rows found for %s/%s/%s/%s", state, district, soil_type, crop)
    return tuple()


# ── Row parsing ───────────────────────────────────────────────────────────────

def _parse_into(rows: tuple, missing_keys: list, data: SoilData):
    """
    For each row, if nutrient_key is in missing_keys, set it on data.

    Value resolution (in priority order):
      1. value_range (parsed as mean) — preferred when available
      2. typical_value — fallback when value_range is absent or unparseable
    """
    seen = set()
    for row in rows:
        key = row.get("nutrient_key")
        if key not in missing_keys or key in seen:
            continue

        # Attempt range-based mean; fall back to exact typical_value
        value_range = row.get("value_range")
        method = None
        if value_range:
            try:
                val, method = _parse_range_mean(value_range)
            except (ValueError, AttributeError, TypeError):
                logger.debug(
                    "[MasterTable] Could not parse range '%s' for %s — using typical_value",
                    value_range, key,
                )
                val = row.get("typical_value")
        else:
            val = row.get("typical_value")

        if val is None:
            continue

        data.set(
            key,
            float(val),
            source      = "supabase",
            confidence  = CONFIDENCE_SCORES["supabase"],
            unit        = row.get("unit"),
            ideal_range = row.get("ideal_range"),
            status      = row.get("typical_status"),
            method      = method,   # "range_mean" or None
        )
        seen.add(key)


def _parse_range_mean(range_str: str) -> Tuple[float, str]:
    """
    Parse a range string and return (mean_value, method_tag).

    Supports both hyphen (-) and en-dash (–) separators.
    Examples: "80–160" → (120.0, "range_mean")
              "7.5-8.2" → (7.85, "range_mean")

    Raises ValueError if the string cannot be parsed as a two-part range.
    """
    # Normalise separators: en-dash and em-dash → hyphen
    normalized = range_str.replace("\u2013", "-").replace("\u2014", "-").strip()
    parts = normalized.split("-")

    if len(parts) != 2:
        raise ValueError(f"Expected 'low-high' format, got: '{range_str}'")

    lo = float(parts[0].strip())
    hi = float(parts[1].strip())
    mean = round((lo + hi) / 2, 4)
    return mean, "range_mean"
