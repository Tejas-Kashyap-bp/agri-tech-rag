"""
Deterministic nutrition/fertilizer guardrail layer for Engine 3.

Follows the same "decorator" pattern as guardrails.py (E4) and
satellite_layer.py (E3 satellite advisory):
- Runs after the E3 LLM call, before the result is returned.
- Adds a single "nutrition_guardrails" block to fertilizer/details.
- Never overwrites existing keys. Never raises on missing data.
- Completely independent of E4 pest/disease logic.

Guardrails:
  1. rain_before_fertilizer_guardrail  — delay if >10 mm rain expected in 24 h
  2. cold_snow_fertilizer_guardrail    — block if snow or temp < 5°C in 24 h
  3. hail_recovery_nutrition_guardrail — switch to recovery mode after hail
  4. frost_damage_hold_guardrail       — hold if frost (min temp ≤ 0°C or frost indicator)

Final priority (highest wins):
  HAIL_RECOVERY_MODE > BLOCKED > HOLD > DELAYED > ALLOWED > UNKNOWN
"""

import logging
from typing import Any, Optional, Union

# Re-use the shared weather helpers and detection functions already defined
# in guardrails.py so we stay DRY without duplicating any logic.
from app.advisory.guardrails import (
    _hourly,
    _safe_float,
    _wmo_in_list,
    _HAIL_WMO,
    _SNOW_WMO,
)

log = logging.getLogger("advisory.nutrition_guardrails")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

_HEAVY_RAIN_MM_24H: float = 10.0    # total rain in 24 h that causes leaching
_RAIN_WINDOW_HOURS: int = 24        # hours to look ahead for rain
_COLD_TEMP_THRESHOLD_C: float = 5.0 # min temp below which uptake is too low
_TEMP_WINDOW_HOURS: int = 24        # hours to look ahead for cold snap
_FROST_TEMP_THRESHOLD_C: float = 0.0  # freezing point; ≤ this = frost
_FROST_WINDOW_HOURS: int = 24         # hours of temperature history to scan


# ─── HAIL DETECTION (nutrition-scoped, weather + extra only) ─────────────────
# NOTE: At E3 decoration time, E4.1 has NOT finished yet (Tier-2 is parallel).
# So we cannot read E4.1 yield_signals here. We detect hail directly from
# context.extra (explicit flag the caller may supply) and from weather data.

def _detect_hail_for_nutrition(
    weather: Optional[dict],
    context_extra: Optional[dict],
) -> Union[bool, str]:
    """
    Returns True / False / "UNKNOWN".

    Sources checked (in order):
      1. context.extra explicit hail flag
      2. weather condition text
      3. WMO hail codes (27, 89, 96, 99)
    """
    ex = context_extra or {}

    # 1 — explicit flag from caller
    for key in ("hail_event", "hail_detected", "has_hail", "hail"):
        v = ex.get(key)
        if v is not None:
            if isinstance(v, bool):
                return v
            if isinstance(v, str) and v.lower() in ("true", "yes", "1"):
                return True
            if isinstance(v, (int, float)) and v > 0:
                return True

    if not weather:
        return "UNKNOWN"

    # 2 — weather dict explicit flag
    for key in ("hail_event", "hail_detected", "has_hail", "hail"):
        v = weather.get(key)
        if v is not None:
            if isinstance(v, bool):
                return v
            if isinstance(v, str) and v.lower() in ("true", "yes", "1"):
                return True
            if isinstance(v, (int, float)) and v > 0:
                return True

    # 3 — condition text
    cond = str(weather.get("condition") or weather.get("weather_condition") or "")
    if "hail" in cond.lower():
        return True

    # 4 — WMO codes
    result = _wmo_in_list(weather, _HAIL_WMO)
    if result is True:
        return True
    if result is False:
        return False
    return "UNKNOWN"


# ─── SNOW DETECTION (nutrition-scoped) ───────────────────────────────────────

def _detect_snow_for_nutrition(weather: dict) -> Union[bool, str]:
    """
    Returns True / False / "UNKNOWN".
    Replicates the multi-source snow detection from guardrails.py.
    """
    # Snowfall field
    snowfall = _hourly(weather, "snowfall", "snow_depth", "snow")
    if snowfall and any((_safe_float(s) or 0.0) > 0 for s in snowfall):
        return True

    # precipitation_type
    prec_type = _hourly(weather, "precipitation_type")
    if prec_type:
        for pt in prec_type:
            if isinstance(pt, str) and "snow" in pt.lower():
                return True
            if isinstance(pt, (int, float)) and int(pt) == 3:
                return True

    # Condition text
    cond = str(weather.get("condition") or weather.get("weather_condition") or "")
    if "snow" in cond.lower():
        return True

    # WMO codes
    result = _wmo_in_list(weather, _SNOW_WMO)
    if result is True:
        return True

    # Temperature + precipitation fallback: T ≤ 2°C AND precip > 0
    temps = _hourly(weather, "temperature_2m", "temperature", "temp")
    precip = _hourly(weather, "precipitation", "rainfall", "rain")
    if temps and precip:
        n = min(len(temps), len(precip))
        for i in range(n):
            t = _safe_float(temps[i])
            p = _safe_float(precip[i])
            if t is not None and p is not None and t <= 2.0 and p > 0:
                return True

    if result is False:
        return False
    if temps:
        return False
    return "UNKNOWN"


# ─── GUARDRAIL 1 — RAIN BEFORE FERTILIZER ────────────────────────────────────

def _run_rain_before_fertilizer(
    weather: Optional[dict],
    fertilizer_recommended: bool,
) -> dict:
    """
    Block/delay fertilizer if total forecast rainfall in next 24 h ≥ 10 mm.
    Heavy rain leaches nutrients before the tree can absorb them.
    """
    if not fertilizer_recommended:
        return {
            "status": "NOT_APPLICABLE",
            "total_rain_next_24h_mm": None,
            "action": "NOT_APPLICABLE",
            "timing_priority": "NONE",
            "reason": "Fertilizer not recommended — rain check not applicable.",
            "confidence": "HIGH",
        }

    if not weather:
        return {
            "status": "UNKNOWN",
            "total_rain_next_24h_mm": None,
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "reason": "Weather data not available.",
            "confidence": "LOW",
        }

    try:
        rain_list = _hourly(weather, "precipitation", "rainfall", "rain")
        if not rain_list:
            return {
                "status": "UNKNOWN",
                "total_rain_next_24h_mm": None,
                "action": "UNKNOWN",
                "timing_priority": "UNKNOWN",
                "reason": "Rainfall forecast data not found.",
                "confidence": "LOW",
            }

        window = rain_list[:_RAIN_WINDOW_HOURS]
        total = round(sum(_safe_float(r) or 0.0 for r in window), 2)

        if total >= _HEAVY_RAIN_MM_24H:
            return {
                "status": "DELAY_FERTILIZER_DUE_TO_RAIN",
                "total_rain_next_24h_mm": total,
                "action": "DELAY_FERTILIZER",
                "timing_priority": "DELAY",
                "reason": (
                    f"Heavy rain expected ({total} mm in next 24 h). "
                    "Fertilizer may leach away before tree uptake."
                ),
                "confidence": "HIGH",
            }
        return {
            "status": "FERTILIZER_RAIN_SAFE",
            "total_rain_next_24h_mm": total,
            "action": "ALLOW_FERTILIZER",
            "timing_priority": "SAFE",
            "reason": (
                f"Expected rainfall ({total} mm) is below leaching threshold. "
                "Safe to apply fertilizer."
            ),
            "confidence": "HIGH",
        }
    except Exception:
        log.warning("rain_before_fertilizer error", exc_info=True)
        return {
            "status": "UNKNOWN",
            "total_rain_next_24h_mm": None,
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "reason": "Error during rainfall computation.",
            "confidence": "LOW",
        }


# ─── GUARDRAIL 2 — COLD / SNOW ────────────────────────────────────────────────

def _run_cold_snow_fertilizer(
    weather: Optional[dict],
    fertilizer_recommended: bool,
) -> dict:
    """
    Block fertilizer if snow is present OR minimum temperature in next 24 h
    drops below 5°C. Cold soil and frozen roots cannot absorb nutrients.
    """
    if not fertilizer_recommended:
        return {
            "status": "NOT_APPLICABLE",
            "snow_event": None,
            "min_temperature_next_24h_c": None,
            "action": "NOT_APPLICABLE",
            "timing_priority": "NONE",
            "reason": "Fertilizer not recommended — cold/snow check not applicable.",
            "confidence": "HIGH",
        }

    if not weather:
        return {
            "status": "UNKNOWN",
            "snow_event": "UNKNOWN",
            "min_temperature_next_24h_c": None,
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "reason": "Weather data not available.",
            "confidence": "LOW",
        }

    try:
        snow = _detect_snow_for_nutrition(weather)

        temp_list = _hourly(weather, "temperature_2m", "temperature", "temp")
        min_temp: Optional[float] = None
        if temp_list:
            window = [_safe_float(t) for t in temp_list[:_TEMP_WINDOW_HOURS]]
            valid = [t for t in window if t is not None]
            min_temp = round(min(valid), 2) if valid else None

        # Block conditions: snow OR temp below threshold
        snow_block = snow is True
        temp_block = min_temp is not None and min_temp < _COLD_TEMP_THRESHOLD_C
        data_missing = snow == "UNKNOWN" and min_temp is None

        if data_missing:
            return {
                "status": "UNKNOWN",
                "snow_event": snow,
                "min_temperature_next_24h_c": min_temp,
                "action": "UNKNOWN",
                "timing_priority": "UNKNOWN",
                "reason": "Temperature and snow data not available.",
                "confidence": "LOW",
            }

        if snow_block or temp_block:
            reasons = []
            if snow_block:
                reasons.append("snow detected")
            if temp_block:
                reasons.append(f"minimum temperature ({min_temp}°C) is below {_COLD_TEMP_THRESHOLD_C}°C")
            return {
                "status": "BLOCK_FERTILIZER_DUE_TO_COLD_OR_SNOW",
                "snow_event": snow,
                "min_temperature_next_24h_c": min_temp,
                "action": "AVOID_FERTILIZER",
                "timing_priority": "BLOCK",
                "reason": (
                    "Cold or snow conditions reduce nutrient uptake — "
                    + ", ".join(reasons) + "."
                ),
                "confidence": "HIGH",
            }

        return {
            "status": "FERTILIZER_TEMPERATURE_SAFE",
            "snow_event": snow,
            "min_temperature_next_24h_c": min_temp,
            "action": "ALLOW_FERTILIZER",
            "timing_priority": "SAFE",
            "reason": (
                "No snow and temperature is above threshold. "
                "Nutrient uptake conditions are suitable."
            ),
            "confidence": "HIGH" if min_temp is not None else "MEDIUM",
        }
    except Exception:
        log.warning("cold_snow_fertilizer error", exc_info=True)
        return {
            "status": "UNKNOWN",
            "snow_event": "UNKNOWN",
            "min_temperature_next_24h_c": None,
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "reason": "Error during cold/snow computation.",
            "confidence": "LOW",
        }


# ─── GUARDRAIL 3 — HAIL RECOVERY NUTRITION MODE ──────────────────────────────

def _run_hail_recovery_nutrition(
    weather: Optional[dict],
    fertilizer_recommended: bool,
    context_extra: Optional[dict],
) -> dict:
    """
    If hail is detected, pause the normal fertilizer plan and switch to
    recovery nutrition mode. Hail-stressed trees should not receive aggressive
    nitrogen; focus shifts to wound healing and plant recovery support.
    """
    try:
        hail = _detect_hail_for_nutrition(weather, context_extra)

        if hail is True:
            return {
                "status": "HAIL_RECOVERY_MODE",
                "hail_event": True,
                "nutrition_mode": "RECOVERY",
                "action": "PAUSE_NORMAL_FERTILIZER_PLAN",
                "timing_priority": "RECOVERY_FIRST",
                "reason": (
                    "Hail damage detected. Normal fertilizer plan should be paused. "
                    "Recovery nutrition mode should be used instead."
                ),
                "confidence": "HIGH",
            }
        if hail is False:
            return {
                "status": "NO_HAIL_RECOVERY_NEEDED",
                "hail_event": False,
                "nutrition_mode": "NORMAL",
                "action": "NORMAL_FERTILIZER_PLAN_ALLOWED",
                "timing_priority": "SAFE",
                "reason": "No hail event detected. Normal fertilizer plan can proceed.",
                "confidence": "HIGH",
            }
        # UNKNOWN
        return {
            "status": "UNKNOWN",
            "hail_event": "UNKNOWN",
            "nutrition_mode": "UNKNOWN",
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "reason": "Hail event status could not be determined from available data.",
            "confidence": "LOW",
        }
    except Exception:
        log.warning("hail_recovery_nutrition error", exc_info=True)
        return {
            "status": "UNKNOWN",
            "hail_event": "UNKNOWN",
            "nutrition_mode": "UNKNOWN",
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "reason": "Error during hail detection.",
            "confidence": "LOW",
        }


# ─── FROST DETECTION ─────────────────────────────────────────────────────────

def _detect_frost_event(
    weather: Optional[dict],
    context_extra: Optional[dict],
) -> Union[bool, str]:
    """
    Returns True / False / "UNKNOWN".

    Sources checked (in order):
      1. context.extra explicit frost flag
      2. weather dict explicit frost flag or condition text containing "frost"
      3. minimum temperature in hourly data <= 0°C (within FROST_WINDOW_HOURS)

    Returns "UNKNOWN" only when all temperature data is absent AND no explicit
    signal was found.
    """
    ex = context_extra or {}

    # 1 — explicit flag from caller (context.extra)
    for key in ("frost_event", "frost_detected", "has_frost", "frost"):
        v = ex.get(key)
        if v is not None:
            if isinstance(v, bool):
                return v
            if isinstance(v, str) and v.lower() in ("true", "yes", "1"):
                return True
            if isinstance(v, str) and v.lower() in ("false", "no", "0"):
                return False
            if isinstance(v, (int, float)):
                return v > 0

    if not weather:
        return "UNKNOWN"

    # 2 — weather dict explicit frost flag
    for key in ("frost_event", "frost_detected", "has_frost", "frost"):
        v = weather.get(key)
        if v is not None:
            if isinstance(v, bool):
                return v
            if isinstance(v, str) and v.lower() in ("true", "yes", "1"):
                return True
            if isinstance(v, (int, float)) and v > 0:
                return True

    # 2b — condition text
    cond = str(weather.get("condition") or weather.get("weather_condition") or "")
    if "frost" in cond.lower():
        return True

    # 3 — temperature-based detection
    temps = _hourly(weather, "temperature_2m", "temperature", "temp")
    if not temps:
        return "UNKNOWN"

    window = [_safe_float(t) for t in temps[:_FROST_WINDOW_HOURS]]
    valid = [t for t in window if t is not None]
    if not valid:
        return "UNKNOWN"

    return min(valid) <= _FROST_TEMP_THRESHOLD_C


# ─── GUARDRAIL 4 — FROST DAMAGE HOLD ─────────────────────────────────────────

def _run_frost_damage_hold(
    weather: Optional[dict],
    fertilizer_recommended: bool,
    context_extra: Optional[dict],
) -> dict:
    """
    Hold fertilizer if a recent frost event is detected.

    Frost can injure plant tissues and impair nutrient uptake pathways.
    Applying fertilizer onto frost-stressed tissue may cause further stress
    rather than aiding recovery.  The advisory is to assess damage first.

    frost_event_recent detection criteria (any one is sufficient):
      - min temperature in last 24 h <= 0°C
      - explicit frost flag in context.extra or weather dict
      - weather condition text contains "frost"
    """
    if not fertilizer_recommended:
        return {
            "status": "NOT_APPLICABLE",
            "frost_event_recent": None,
            "action": "NOT_APPLICABLE",
            "timing_priority": "NONE",
            "nutrition_mode": "NORMAL",
            "reason": "Fertilizer not recommended — frost check not applicable.",
            "confidence": "HIGH",
        }

    try:
        frost = _detect_frost_event(weather, context_extra)

        if frost is True:
            return {
                "status": "HOLD_FERTILIZER_DUE_TO_FROST",
                "frost_event_recent": True,
                "action": "HOLD_FERTILIZER",
                "timing_priority": "HOLD",
                "nutrition_mode": "ASSESS_DAMAGE_FIRST",
                "reason": (
                    "Recent frost damage may have injured plant tissues. "
                    "Fertilizer should be applied only after assessing plant recovery."
                ),
                "advisory_message": (
                    "Do not apply fertilizer immediately after frost. "
                    "First assess plant damage and recovery condition."
                ),
                "confidence": "HIGH",
            }

        if frost is False:
            return {
                "status": "NO_FROST_RESTRICTION",
                "frost_event_recent": False,
                "action": "ALLOW_FERTILIZER",
                "timing_priority": "SAFE",
                "nutrition_mode": "NORMAL",
                "reason": "No frost event detected. Fertilizer application is not restricted by frost.",
                "confidence": "HIGH",
            }

        # UNKNOWN
        return {
            "status": "UNKNOWN",
            "frost_event_recent": "UNKNOWN",
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "nutrition_mode": "UNKNOWN",
            "reason": "Frost event status could not be determined from available data.",
            "confidence": "LOW",
        }

    except Exception:
        log.warning("frost_damage_hold error", exc_info=True)
        return {
            "status": "UNKNOWN",
            "frost_event_recent": "UNKNOWN",
            "action": "UNKNOWN",
            "timing_priority": "UNKNOWN",
            "nutrition_mode": "UNKNOWN",
            "reason": "Error during frost detection.",
            "confidence": "LOW",
        }


# ─── FINAL DECISION AGGREGATOR ────────────────────────────────────────────────

def _compute_final_decision(
    rain_g: dict,
    cold_g: dict,
    hail_g: dict,
    frost_g: dict,
) -> dict:
    """
    Combine all four guardrail outcomes using priority order:
      1. HAIL_RECOVERY_MODE          → RECOVERY_MODE  (highest)
      2. BLOCK_FERTILIZER_...        → BLOCKED
      3. HOLD_FERTILIZER_DUE_TO_FROST → HOLD
      4. DELAY_FERTILIZER_...        → DELAYED
      5. All SAFE / NOT_APPLICABLE   → ALLOWED
      6. Cannot decide               → UNKNOWN
    """
    hail_status  = hail_g.get("status",  "UNKNOWN")
    cold_status  = cold_g.get("status",  "UNKNOWN")
    frost_status = frost_g.get("status", "UNKNOWN")
    rain_status  = rain_g.get("status",  "UNKNOWN")

    # Priority 1 — hail recovery
    if hail_status == "HAIL_RECOVERY_MODE":
        return {
            "fertilizer_application_status": "RECOVERY_MODE",
            "primary_blocking_reason": hail_g.get("reason", ""),
            "message": (
                "Normal fertilizer plan should be paused because hail damage was detected. "
                "Use recovery nutrition mode as per local POP."
            ),
            "notes": [
                "Do not push aggressive nitrogen immediately after hail.",
                "Focus on plant recovery support and wound healing.",
                "Follow local horticulture POP for post-hail recovery nutrition.",
            ],
        }

    # Priority 2 — cold/snow block
    if cold_status == "BLOCK_FERTILIZER_DUE_TO_COLD_OR_SNOW":
        return {
            "fertilizer_application_status": "BLOCKED",
            "primary_blocking_reason": cold_g.get("reason", ""),
            "message": "Fertilizer should not be applied now due to snow or very cold conditions.",
            "notes": [
                "Nutrient uptake is very low below 5°C.",
                "Snow prevents fertilizer from reaching the root zone effectively.",
                "Resume fertilizer application when conditions warm up.",
            ],
        }

    # Priority 3 — frost hold
    if frost_status == "HOLD_FERTILIZER_DUE_TO_FROST":
        return {
            "fertilizer_application_status": "HOLD",
            "primary_blocking_reason": frost_g.get("reason", ""),
            "message": (
                "Fertilizer application should be held following a recent frost event. "
                "Assess plant tissue condition before resuming."
            ),
            "notes": [
                "Frost may have injured plant tissues, impairing nutrient uptake.",
                "Applying fertilizer onto frost-stressed tissue may cause further stress.",
                "Assess plant recovery first; resume only when tissues appear healthy.",
            ],
        }

    # Priority 4 — rain delay
    if rain_status == "DELAY_FERTILIZER_DUE_TO_RAIN":
        return {
            "fertilizer_application_status": "DELAYED",
            "primary_blocking_reason": rain_g.get("reason", ""),
            "message": (
                "Fertilizer should be delayed because heavy rainfall is expected "
                "and nutrients may leach away."
            ),
            "notes": [
                "Heavy rain (≥10 mm/24 h) can wash nutrients below the root zone.",
                "Wait for the rain to pass before applying fertilizer.",
                "Light irrigation after fertilizer application is acceptable.",
            ],
        }

    # Priority 5 — all safe
    # Rain, cold, and frost UNKNOWN = we cannot confirm safety → stays UNKNOWN.
    # Hail UNKNOWN = no evidence of hail → treat as safe (RECOVERY_MODE requires
    # positive confirmation, not just uncertainty).
    # Frost UNKNOWN = insufficient data → cannot confirm safety → stays UNKNOWN.
    rain_cold_frost_safe = {
        "FERTILIZER_RAIN_SAFE", "NOT_APPLICABLE", "FERTILIZER_TEMPERATURE_SAFE",
        "NO_HAIL_RECOVERY_NEEDED", "NO_FROST_RESTRICTION",
    }
    hail_safe = {"NO_HAIL_RECOVERY_NEEDED", "UNKNOWN", "NOT_APPLICABLE"}
    all_safe = (
        rain_status  in rain_cold_frost_safe
        and cold_status  in rain_cold_frost_safe
        and frost_status in rain_cold_frost_safe
        and hail_status  in hail_safe
    )
    if all_safe:
        return {
            "fertilizer_application_status": "ALLOWED",
            "primary_blocking_reason": None,
            "message": "Fertilizer application is allowed based on current weather guardrails.",
            "notes": [
                "Apply fertilizer as per the Engine 3 schedule recommendation.",
                "Monitor weather for changes that may affect application timing.",
            ],
        }

    # Priority 6 — cannot decide (at least one UNKNOWN, no blocking confirmed)
    return {
        "fertilizer_application_status": "UNKNOWN",
        "primary_blocking_reason": "One or more guardrails could not be evaluated due to missing data.",
        "message": "Fertilizer guardrail decision could not be completed due to missing data.",
        "notes": [
            "Check weather data availability.",
            "Use local agronomist judgment until data is available.",
        ],
    }


# ─── MAIN DECORATOR ───────────────────────────────────────────────────────────

def decorate_with_nutrition_guardrails(
    e3_result: dict[str, Any],
    weather: Optional[dict],
    context_extra: Optional[dict],
) -> None:
    """
    Add nutrition_guardrails block to E3 fertilizer result in-place.

    Contract (same as all other decorators):
    - Additive only: uses setdefault — never overwrites existing keys.
    - Never raises: all errors are caught, logged, and returned as UNKNOWN.
    - E4 pest/disease logic is completely untouched.
    """
    try:
        if not isinstance(e3_result.get("details"), dict):
            e3_result["details"] = {}

        ex = context_extra or {}

        # fertilizer_recommended: explicit caller override wins, else default True
        # (conservative — guardrails should always evaluate unless told otherwise)
        if "fertilizer_recommended" in ex:
            fert_rec = bool(ex["fertilizer_recommended"])
        else:
            fert_rec = True

        # ── Guardrail 1: Rain before fertilizer ───────────────────────────
        rain_g = _run_rain_before_fertilizer(weather, fert_rec)

        # ── Guardrail 2: Cold / Snow ───────────────────────────────────────
        cold_g = _run_cold_snow_fertilizer(weather, fert_rec)

        # ── Guardrail 3: Hail recovery ────────────────────────────────────
        hail_g = _run_hail_recovery_nutrition(weather, fert_rec, ex)

        # ── Guardrail 4: Frost damage hold ────────────────────────────────
        frost_g = _run_frost_damage_hold(weather, fert_rec, ex)

        # ── Final combined decision ────────────────────────────────────────
        final = _compute_final_decision(rain_g, cold_g, hail_g, frost_g)

        nutrition_guardrails = {
            "enabled": True,
            "rain_before_fertilizer_guardrail": rain_g,
            "cold_snow_fertilizer_guardrail": cold_g,
            "hail_recovery_nutrition_guardrail": hail_g,
            "frost_damage_hold_guardrail": frost_g,
            "final_nutrition_decision": final,
        }

        e3_result["details"].setdefault("nutrition_guardrails", nutrition_guardrails)

    except Exception:
        log.exception("decorate_with_nutrition_guardrails: unexpected top-level error")
