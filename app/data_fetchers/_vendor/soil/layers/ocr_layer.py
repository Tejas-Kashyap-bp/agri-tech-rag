"""
layers/ocr_layer.py — Layer 1: OCR + LLM soil report parser.

Flow:
  1. Tesseract OCR extracts raw text from the report image/PDF.
  2. LLM (Groq) extracts raw values + original units from the text.
  3. Python _convert_to_canonical() normalizes to canonical units
     (N/P/K → kg/ha, OC → %, micronutrients → ppm).
  4. If LLM fails (network, quota, bad JSON) → fall back to regex parsing.
"""

import json
import re
import io
import logging
from pathlib import Path
from typing import Optional, Union

import pytesseract
from PIL import Image

from ..models import CONFIDENCE_SCORES, SoilData

logger = logging.getLogger(__name__)


# Tesseract config: OEM 3 = default engine, PSM 3 = fully automatic page segmentation
_TESS_CONFIG = r"--oem 3 --psm 3"


# ── LLM extraction prompt ────────────────────────────────────────────────────

_LLM_PROMPT = """\
You are an expert in interpreting Indian soil test reports.

Extract the EXACT numeric values as printed on the report. Do NOT convert units yourself.
Report the unit exactly as it appears (e.g. "kg/ha", "kg/acre", "mg/kg", "ppm", "g/kg", "%", "dS/m").

Extract ALL of the following if present:
- Nitrogen (N) — available nitrogen
- Phosphorus (P) — available phosphorus
- Potassium (K) — available potassium
- Organic Carbon (OC)
- pH
- EC (Electrical Conductivity)
- Zinc (Zn)
- Iron (Fe)
- Manganese (Mn)
- Copper (Cu)
- Boron (B)
- Sulphur (S)

Return ONLY compact JSON (no extra whitespace or alignment):
{{"nitrogen_N":{{"value":<number or null>,"unit":"<unit as printed>"}},"phosphorus_P":{{"value":<number or null>,"unit":"<unit as printed>"}},"potassium_K":{{"value":<number or null>,"unit":"<unit as printed>"}},"organic_carbon_OC":{{"value":<number or null>,"unit":"<unit as printed>"}},"ph":{{"value":<number or null>,"unit":"pH"}},"ec":{{"value":<number or null>,"unit":"<unit as printed>"}},"zinc_Zn":{{"value":<number or null>,"unit":"<unit as printed>"}},"iron_Fe":{{"value":<number or null>,"unit":"<unit as printed>"}},"manganese_Mn":{{"value":<number or null>,"unit":"<unit as printed>"}},"copper_Cu":{{"value":<number or null>,"unit":"<unit as printed>"}},"boron_B":{{"value":<number or null>,"unit":"<unit as printed>"}},"sulphur_S":{{"value":<number or null>,"unit":"<unit as printed>"}}}}

Do not explain anything. Output only the JSON object.

Soil report text:
{ocr_text}
"""

# Maps LLM output keys → canonical SoilData keys
_LLM_KEY_MAP = {
    "nitrogen_N":        "nitrogen_N",
    "phosphorus_P":      "phosphorus_P",
    "potassium_K":       "potassium_K",
    "organic_carbon_OC": "organic_carbon_OC",
    "ph":                "soil_pH",
    "ec":                "ec_EC",
    "zinc_Zn":           "zinc_Zn",
    "iron_Fe":           "iron_Fe",
    "manganese_Mn":      "manganese_Mn",
    "copper_Cu":         "copper_Cu",
    "boron_B":           "boron_B",
    "sulphur_S":         "sulphur_S",
}

# Canonical units per key — what we normalize to after LLM extraction
_CANONICAL_UNITS = {
    "nitrogen_N":        "kg/ha",
    "phosphorus_P":      "kg/ha",
    "potassium_K":       "kg/ha",
    "organic_carbon_OC": "%",
    "soil_pH":           None,
    "ec_EC":             "dS/m",
    "zinc_Zn":           "ppm",
    "iron_Fe":           "ppm",
    "manganese_Mn":      "ppm",
    "copper_Cu":         "ppm",
    "boron_B":           "ppm",
    "sulphur_S":         "ppm",
}

# ── Unit conversion to canonical ────────────────────────────────────────────
# LLM extracts raw values + units from the report; we convert in Python.

# Conversion factors → kg/ha (for N, P, K)
_TO_KG_HA = {
    "kg/ha":  1.0,
    "kg/hec": 1.0,
    "kg/acre": 2.471,
    "mg/kg":  2.24,     # standard 0–15 cm, bulk density ~1.12 g/cm³
    "ppm":    2.24,
    "g/kg":   2240.0,   # mg/kg × 1000, then × 2.24
}

# Conversion factors → % (for OC)
_TO_PERCENT = {
    "%":    1.0,
    "g/kg": 0.1,    # g/kg ÷ 10 → %
}

# Conversion factors → ppm (for micronutrients)
_TO_PPM = {
    "ppm":   1.0,
    "mg/kg": 1.0,   # 1 mg/kg ≡ 1 ppm (exact equivalence)
    "mg/l":  1.0,   # often used interchangeably on Indian reports
}

# Keys that use ppm as canonical unit
_PPM_KEYS = {"zinc_Zn", "iron_Fe", "manganese_Mn", "copper_Cu", "boron_B", "sulphur_S"}


def _convert_to_canonical(value: float, raw_unit: str, canonical_key: str) -> Optional[float]:
    """Convert a raw LLM-extracted value to canonical units. Returns None if unit unknown."""
    raw_unit_lower = raw_unit.strip().lower() if raw_unit else ""

    if canonical_key in ("nitrogen_N", "phosphorus_P", "potassium_K"):
        factor = _TO_KG_HA.get(raw_unit_lower)
        if factor is None:
            logger.warning("[OCR] Unknown unit '%s' for %s — skipping conversion", raw_unit, canonical_key)
            return None
        return round(value * factor, 2)

    if canonical_key == "organic_carbon_OC":
        factor = _TO_PERCENT.get(raw_unit_lower)
        if factor is None:
            logger.warning("[OCR] Unknown unit '%s' for OC — skipping conversion", raw_unit)
            return None
        return round(value * factor, 4)

    if canonical_key in _PPM_KEYS:
        factor = _TO_PPM.get(raw_unit_lower)
        if factor is None:
            logger.warning("[OCR] Unknown unit '%s' for %s — skipping conversion", raw_unit, canonical_key)
            return None
        return round(value * factor, 4)

    # pH, EC — no conversion needed
    return value


# ── Regex patterns (fallback) ────────────────────────────────────────────────

FIELD_PATTERNS = {
    "soil_pH": [
        r"soil\s*ph[^\d]{0,30}([\d]+\.?[\d]*)",
        r"\bph\b[^\d]{0,30}([\d]+\.?[\d]*)",
    ],
    "nitrogen_N": [
        r"available\s*nit(?:rogen)?[^\d]{0,30}([\d]+\.?[\d]*)",
        r"nit(?:rogen)?[^\d]{0,30}([\d]+\.?[\d]*)",
        r"\bn\b[^\d]{0,20}([\d]+\.?[\d]*)\s*(?:kg|ppm|%)",
    ],
    "phosphorus_P": [
        r"available\s*ph(?:osphor(?:us|ate)|osphate)?[^\d]{0,30}([\d]+\.?[\d]*)",
        r"ph(?:osphor(?:us|ate))[^\d]{0,30}([\d]+\.?[\d]*)",
        r"\bp\b[^\d]{0,20}([\d]+\.?[\d]*)\s*(?:kg|ppm|%)",
    ],
    "potassium_K": [
        r"available\s*(?:pot(?:assium|ash)?|k)[^\d]{0,30}([\d]+\.?[\d]*)",
        r"pot(?:assium|ash)[^\d]{0,30}([\d]+\.?[\d]*)",
        r"\bk\b[^\d]{0,20}([\d]+\.?[\d]*)\s*(?:kg|ppm|%)",
    ],
    "organic_carbon_OC": [
        r"organic\s*carb(?:on)?[^\d]{0,30}([\d]+\.?[\d]*)",
        r"\boc\b[^\d]{0,20}([\d]+\.?[\d]*)",
    ],
    "zinc_Zn":     [r"zinc[^\d]{0,20}([\d]+\.?[\d]*)", r"\bzn\b[^\d]{0,20}([\d]+\.?[\d]*)"],
    "iron_Fe":     [r"iron[^\d]{0,20}([\d]+\.?[\d]*)", r"\bfe\b[^\d]{0,20}([\d]+\.?[\d]*)"],
    "manganese_Mn":[r"manganese[^\d]{0,20}([\d]+\.?[\d]*)", r"\bmn\b[^\d]{0,20}([\d]+\.?[\d]*)"],
    "copper_Cu":   [r"copper[^\d]{0,20}([\d]+\.?[\d]*)", r"\bcu\b[^\d]{0,20}([\d]+\.?[\d]*)"],
    "boron_B":     [r"boron[^\d]{0,20}([\d]+\.?[\d]*)", r"\bb\b[^\d]{0,20}([\d]+\.?[\d]*)\s*(?:kg|ppm|%)"],
    "sulphur_S":   [r"sul(?:ph|f)ur[^\d]{0,20}([\d]+\.?[\d]*)", r"\bs\b[^\d]{0,20}([\d]+\.?[\d]*)\s*(?:kg|ppm|%)"],
}

STATUS_PATTERNS = {
    "Low":    r"\b(low|deficient|deficiency)\b",
    "High":   r"\b(high|excess|excessive)\b",
    "Normal": r"\b(normal|adequate|sufficient|optimum|optimal)\b",
}

_OCR_CORRECTIONS = {
    r"\b0rganic\b":    "organic",
    r"\bp[Hh]\b":      "pH",
    r"\bN1trogen\b":   "Nitrogen",
    r"\bZ1nc\b":       "Zinc",
    r"\bFe\s*rrum\b":  "Iron",
    r"[\u2013\u2014]": "-",
}


# ── Public function ───────────────────────────────────────────────────────────

def extract_from_report(
    report_path:  Union[str, Path] = None,
    report_bytes: bytes = None,
) -> SoilData:
    """
    Extract soil values from a report image/PDF.

    Primary path  : OCR → LLM → convert → SoilData (canonical units)
    Fallback path : OCR → regex → SoilData (values as-extracted, no unit conversion)

    Args:
        report_path:  Path to JPG / PNG / single-page PDF.
        report_bytes: Raw image bytes (alternative to path).

    Returns:
        SoilData with source="farmer" for every field successfully extracted.
    """
    data = SoilData()

    if not report_path and not report_bytes:
        logger.info("[OCR] No report provided — skipping layer 1.")
        return data

    try:
        raw_text   = _run_ocr(report_path, report_bytes)
        clean_text = _preprocess(raw_text)
        logger.debug("[OCR] Preprocessed text (%d chars): %s", len(clean_text), clean_text[:400])

        # ── Primary: LLM extraction ──────────────────────────────────────────
        llm_success = _parse_with_llm(clean_text, data)

        if llm_success:
            logger.info("[OCR] LLM extracted %d fields: %s", len(data.nutrients), list(data.nutrients.keys()))
        else:
            # ── Fallback: regex extraction ───────────────────────────────────
            logger.info("[OCR] LLM failed — falling back to regex parser")
            _parse_with_regex(clean_text, data)
            logger.info("[OCR] Regex extracted %d fields: %s", len(data.nutrients), list(data.nutrients.keys()))

    except Exception as exc:
        logger.warning("[OCR] Extraction failed: %s", exc, exc_info=True)

    return data


# ── LLM extraction ────────────────────────────────────────────────────────────

def _parse_with_llm(text: str, data: SoilData) -> bool:
    """
    Send OCR text to Groq LLM, parse the JSON response, populate SoilData.

    Returns True if at least one field was successfully extracted.
    Returns False on any error (caller should fall back to regex).
    """
    try:
        from core.llm.groq_client import call_llm
    except ImportError as e:
        logger.warning("[OCR] Cannot import groq_client (%s) — skipping LLM", e)
        return False

    try:
        prompt   = _LLM_PROMPT.format(ocr_text=text)
        response = call_llm(prompt)
        logger.debug("[OCR] LLM raw response: %s", response[:500])

        parsed = _parse_llm_json(response)
        if not parsed:
            return False

        # Write extracted values into SoilData (convert to canonical units)
        extracted = 0
        for llm_key, canonical_key in _LLM_KEY_MAP.items():
            record = parsed.get(llm_key, {})
            value  = record.get("value")
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                logger.debug("[OCR] LLM returned non-numeric value for %s: %s", llm_key, value)
                continue

            raw_unit = record.get("unit", "")
            converted = _convert_to_canonical(value, raw_unit, canonical_key)
            if converted is None:
                logger.warning("[OCR] Could not convert %s=%s %s → canonical — skipping", canonical_key, value, raw_unit)
                continue

            if converted != value:
                logger.info("[OCR] Converted %s: %s %s → %s %s",
                            canonical_key, value, raw_unit, converted, _CANONICAL_UNITS.get(canonical_key))

            data.set(
                canonical_key,
                converted,
                source     = "farmer",
                confidence = CONFIDENCE_SCORES["farmer"],
                unit       = _CANONICAL_UNITS.get(canonical_key),
            )
            extracted += 1

        return extracted > 0

    except Exception as exc:
        logger.warning("[OCR] LLM extraction error: %s", exc, exc_info=True)
        return False


def _parse_llm_json(response: str) -> Optional[dict]:
    """
    Extract and parse JSON from LLM response.

    Handles two common LLM response styles:
      - Clean JSON only (ideal case)
      - JSON wrapped in ```json ... ``` markdown block
    """
    text = response.strip()

    # Strip markdown code fences if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if match:
            text = match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("[OCR] LLM JSON parse failed: %s — raw: %s", e, response[:200])
        return None


# ── Regex extraction (fallback) ───────────────────────────────────────────────

def _parse_with_regex(text: str, data: SoilData):
    """Apply FIELD_PATTERNS to preprocessed OCR text (original regex logic)."""
    for key, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                try:
                    value = float(m.group(1))
                except ValueError:
                    continue
                status = _find_status_near(m.start(), text)
                data.set(key, value, source="farmer", confidence=CONFIDENCE_SCORES["farmer"], status=status)
                logger.debug("[OCR] Regex matched %s = %s (status=%s)", key, value, status)
                break


# ── Shared helpers ────────────────────────────────────────────────────────────

def _run_ocr(path, raw_bytes) -> str:
    """Run Tesseract OCR and return extracted text."""
    if raw_bytes:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    else:
        img = Image.open(str(path)).convert("RGB")

    text = pytesseract.image_to_string(img, config=_TESS_CONFIG)
    logger.info("[OCR] Tesseract extracted %d chars from %s", len(text), path or "bytes")
    return text


def _preprocess(text: str) -> str:
    """Normalise raw OCR output: fix mis-reads, lowercase, collapse whitespace."""
    for bad, good in _OCR_CORRECTIONS.items():
        text = re.sub(bad, good, text, flags=re.IGNORECASE)
    text = text.lower()
    text = re.sub(r"\s+",                          " ",  text).strip()
    text = re.sub(r"(?<=\w)\s*[:\-–|=]\s*(?=[\d\w])", " ", text)
    return text


def _find_status_near(match_pos: int, text: str) -> Optional[str]:
    """Look for Low/Normal/High keywords within ±100 chars of a match."""
    window = text[max(0, match_pos - 100): match_pos + 100]
    for status, pattern in STATUS_PATTERNS.items():
        if re.search(pattern, window, re.IGNORECASE):
            return status
    return None
