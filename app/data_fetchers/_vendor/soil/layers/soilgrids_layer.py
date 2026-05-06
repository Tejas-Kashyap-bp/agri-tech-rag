"""
layers/soilgrids_layer.py — Layer 2: ISRIC SoilGrids REST API.

Only called for keys that are still missing after OCR.
Returns a SoilData with those keys filled in (or empty if API fails).

SoilGrids v2 docs: https://rest.isric.org/soilgrids/v2.0/docs
"""

import logging
import requests
from ..models import CONFIDENCE_SCORES, SoilData
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
REQUEST_TIMEOUT = 15  # seconds

# ── Mapping: our nutrient_key → SoilGrids property name ─────────────────────
SOILGRIDS_MAP = {
    "soil_pH":           "phh2o",     # pH × 10  (divide by 10)
    "nitrogen_N":        "nitrogen",  # cg/kg    (divide by 100 → %)
    "organic_carbon_OC": "soc",       # dg/kg    (divide by 10 → g/kg)
    "potassium_K":       None,        # not available in SoilGrids v2
    "phosphorus_P":      None,        # not available in SoilGrids v2
}

# Scale factors to convert SoilGrids raw units → intermediate human-readable units
SCALE = {
    "phh2o":    0.1,    # → pH units
    "nitrogen": 0.01,   # cg/kg → % (total N)
    "soc":      0.1,    # dg/kg → g/kg
}

# Plausible value ranges per SoilGrids property name (post-scaling, pre-canonical).
VALID_RANGE = {
    "phh2o":    (2.0,  12.0),   # pH: anything outside this is physically impossible
    "nitrogen": (0.0,   5.0),   # %: extreme soils top out around 4 %
    "soc":      (0.0, 200.0),   # g/kg: very rich peat ~150 g/kg
}

# ── Post-scaling conversion to canonical units ─────────────────────────────
# After SCALE + VALID_RANGE, convert from SoilGrids-native to pipeline-canonical.
#
# nitrogen: total N (%) → approx available N (kg/ha)
#   Factor 2800 ≈ BD 1.4 g/cm³ × depth 20 cm × 10000 m²/ha ÷ 1000 × 0.01
#   Empirical — matches Indian STCR ranges (0.05% → 140, 0.15% → 420 kg/ha).
#
# soc: g/kg → % (straightforward: ÷ 10)
CANONICAL_CONVERT = {
    "nitrogen": {"factor": 2800.0, "unit": "kg/ha"},
    "soc":      {"factor": 0.1,    "unit": "%"},
    "phh2o":    {"factor": 1.0,    "unit": None},
}

# Preferred depth label to query; fallback to first available depth if absent
PREFERRED_DEPTH = "0-30cm"

# Reverse map for response parsing
REVERSE_MAP = {v: k for k, v in SOILGRIDS_MAP.items() if v}


# ── Retry-enabled HTTP session (singleton) ───────────────────────────────────
#
# FIX (issue 1): previously, HTTPAdapter + Retry were imported and constructed
# but requests.get() was called on the bare module, which creates a throwaway
# session that never uses the adapter.  Mount the adapter on a real Session.
#
def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,        # 1 s, 2 s, 4 s
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,     # we handle status ourselves
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _make_session()
    return _session


# ── Public function ──────────────────────────────────────────────────────────

def fetch_from_soilgrids(
    lat: float,
    lon: float,
    missing_keys: list,
) -> SoilData:
    """
    Fetch soil properties for (lat, lon) from ISRIC SoilGrids.
    Only fetches properties that are in missing_keys AND supported by the API.

    Args:
        lat, lon:      Decimal degrees (WGS84).
        missing_keys:  List of nutrient_key strings still needed.

    Returns:
        SoilData with source="soilgrids" and fetched fields populated.
    """
    data = SoilData()

    # FIX (issue 6): validate coordinates before hitting the API
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        logger.warning(f"[SoilGrids] Invalid coordinates ({lat}, {lon}) — skipping.")
        return data

    # Filter to only keys SoilGrids actually provides
    fetchable = [k for k in missing_keys if SOILGRIDS_MAP.get(k)]
    if not fetchable:
        logger.info("[SoilGrids] No supported keys in missing list — skipping.")
        return data

    sg_properties = [SOILGRIDS_MAP[k] for k in fetchable]

    # FIX (issue 4): requests serialises a list as property[]=phh2o, but
    # SoilGrids requires repeated keys: property=phh2o&property=nitrogen.
    # Pass params as a list of (key, value) tuples to force repeated keys.
    params = [("lon", lon), ("lat", lat), ("depth", PREFERRED_DEPTH), ("value", "mean")]
    params += [("property", p) for p in sg_properties]

    try:
        logger.info(f"[SoilGrids] Requesting {sg_properties} at ({lat}, {lon})")
        resp = _get_session().get(SOILGRIDS_URL, params=params, timeout=REQUEST_TIMEOUT)

        # FIX (issue 3): distinguish 429 rate-limit from other HTTP errors
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "unknown")
            logger.warning(f"[SoilGrids] Rate-limited (429). Retry-After: {retry_after}s")
            return data

        resp.raise_for_status()
        _parse_into(resp.json(), data)
        logger.info(f"[SoilGrids] Got {len(data.nutrients)} fields: {list(data.nutrients.keys())}")

    except requests.exceptions.Timeout:
        logger.warning("[SoilGrids] Request timed out.")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"[SoilGrids] HTTP error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as exc:
        logger.warning(f"[SoilGrids] Unexpected error: {exc}", exc_info=True)

    return data


# ── Internal helpers ─────────────────────────────────────────────────────────

def _parse_into(response: dict, data: SoilData):
    """
    SoilGrids v2 response structure:
    {
      "properties": {
        "layers": [
          {
            "name": "phh2o",
            "depths": [{ "label": "0-30cm", "values": {"mean": 68} }]
          }, ...
        ]
      }
    }
    """
    layers = response.get("properties", {}).get("layers", [])
    for layer in layers:
        api_name = layer.get("name")
        nutrient_key = REVERSE_MAP.get(api_name)
        if not nutrient_key:
            continue

        depths = layer.get("depths", [])
        if not depths:
            continue

        # FIX (issue 2): prefer the documented depth label but fall back to
        # the first available depth if the label is missing in this response.
        depth_entry = next(
            (d for d in depths if d.get("label") == PREFERRED_DEPTH),
            depths[0],
        )
        used_depth = depth_entry.get("label", "unknown")
        if used_depth != PREFERRED_DEPTH:
            logger.debug(f"[SoilGrids] '{PREFERRED_DEPTH}' absent for {api_name}; using '{used_depth}'")

        raw_val = depth_entry.get("values", {}).get("mean")
        if raw_val is None:
            logger.debug(f"[SoilGrids] No mean value for {api_name}")
            continue

        scale = SCALE.get(api_name, 1.0)
        value = round(raw_val * scale, 4)

        # FIX (issue 5): reject physically implausible values
        lo, hi = VALID_RANGE.get(api_name, (None, None))
        if lo is not None and not (lo <= value <= hi):
            logger.warning(
                f"[SoilGrids] {api_name} value {value} outside plausible range "
                f"[{lo}, {hi}] — discarding."
            )
            continue

        # Convert from SoilGrids-native units to canonical pipeline units
        conv = CANONICAL_CONVERT.get(api_name)
        if conv:
            canonical_value = round(value * conv["factor"], 4)
            canonical_unit = conv["unit"]
            if conv["factor"] != 1.0:
                logger.debug(
                    "[SoilGrids] Converted %s: %s → %s %s",
                    nutrient_key, value, canonical_value, canonical_unit,
                )
            value = canonical_value
        else:
            canonical_unit = None

        data.set(nutrient_key, value, source="soilgrids",
                 confidence=CONFIDENCE_SCORES["soilgrids"], unit=canonical_unit)