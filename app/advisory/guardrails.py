"""
Deterministic guardrail layer for E4.1 (Pest Risk) and E4.2 (Pest Cure).

Follows the "decorator" pattern used by satellite_layer.py and yield_layer.py:
adds structured guardrail fields to engine outputs after the LLM runs.
Never overwrites existing keys. Never raises on missing data.

Guardrails:
  1. apple_scab_guardrail          — LWI + LWD + ASRI risk model
  1b. lai_biomass_scab_guardrail   — LAI canopy-density modifier (additive,
       does not modify ASRI/LWD/LWI; produces always-on apple_scab_final)
  2. rain_after_spray_guardrail    — forecast rain wash-off check (12 h)
  3. pre_rain_spray_guardrail      — block spray before imminent rain (12 h)
  4. wind_spray_guardrail          — block/delay spray during high wind (6 h)
  5. scab_prone_interval_guardrail — 12-14 day repeat spray interval in scab zones
  6. hail_damage_guardrail         — hail wound protection + yield signal
  7. snow_pest_risk_guardrail      — snow/post-melt disease risk
"""

import logging
import math
from datetime import date as _date, timedelta
from typing import Any, Optional, Union

log = logging.getLogger("advisory.guardrails")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

_LWI_ALPHA: float = 0.25
_LWI_WET_THRESHOLD: float = 0.70
_RH_WET_THRESHOLD: float = 90.0
_DEW_DELTA_WET_MAX: float = 2.0
_ASRI_A: float = 12.0
_ASRI_B: float = 0.04

_RAIN_WASH_MM: float = 0.5
_RAIN_WASH_HOURS: int = 12

_PRE_RAIN_MM: float = 1.0
_PRE_RAIN_PROB: float = 0.6
_PRE_RAIN_HOURS: int = 12

_WIND_SAFE_KMPH: float = 10.0
_WIND_CAUTION_KMPH: float = 15.0
_WIND_HIGH_KMPH: float = 25.0
_WIND_WINDOW_HOURS: int = 6

_SCAB_PRONE_ZONES = frozenset({"scab_prone", "high_scab", "scab_hotspot", "high_risk"})
_MIN_SPRAY_INTERVAL: int = 12
_MAX_SPRAY_INTERVAL: int = 14

# Keywords for primary scab stage detection (checked against lowercase stage text)
_PRIMARY_SCAB_KW = frozenset({
    "green tip", "green_tip", "greentip",
    "bud break", "bud_break", "budbreak",
    "tight cluster", "tight_cluster",
    "pink bud", "pink_bud", "pinkbud",
    "bloom", "full bloom", "flowering",
    "petal fall", "petal_fall", "petalfall",
    "fruit set", "fruit_set", "fruitset",
    "primary scab",
})
_LATE_STAGE_KW = frozenset({
    "dormant", "dormancy", "harvest", "post-harvest", "post harvest",
    "maturity", "color break", "colour break", "storage",
})

# WMO weather codes
_HAIL_WMO = frozenset({27, 89, 96, 99})
_SNOW_WMO = frozenset({36, 37, 71, 73, 75, 77, 85, 86})


# ─── WEATHER FIELD HELPERS ────────────────────────────────────────────────────

def _hourly(weather: dict, *names: str) -> Optional[list]:
    """Try multiple field name variants to find a non-empty hourly list."""
    hourly = weather.get("hourly") or weather.get("hourly_data") or {}
    for name in names:
        val = hourly.get(name)
        if isinstance(val, list) and val:
            return val
    for name in names:
        val = weather.get(name)
        if isinstance(val, list) and val:
            return val
    return None


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _wmo_in_list(weather: dict, codes: frozenset) -> Optional[bool]:
    """Check hourly and current weathercode lists for any matching WMO code.
    Returns True if found, False if codes exist but none match, None if no code data."""
    current = weather.get("current_weather") or weather.get("current") or {}
    wcode = current.get("weathercode") or current.get("weather_code")
    hourly_codes = _hourly(weather, "weathercode", "weather_code")

    has_data = wcode is not None or bool(hourly_codes)
    if not has_data:
        return None

    if wcode is not None:
        try:
            if int(wcode) in codes:
                return True
        except (TypeError, ValueError):
            pass

    if hourly_codes:
        for c in hourly_codes:
            try:
                if int(c) in codes:
                    return True
            except (TypeError, ValueError):
                continue

    return False


# ─── 1. APPLE SCAB GUARDRAIL ──────────────────────────────────────────────────

def calculate_lwi(temp: float, rh: float, dew_point: Optional[float]) -> float:
    """LWI = (RH/100) * exp(-alpha * (T - Td)); clamped [0,1]."""
    rh_frac = max(0.0, min(1.0, rh / 100.0))
    td = dew_point if dew_point is not None else (temp - (100.0 - rh) / 5.0)
    return max(0.0, min(1.0, rh_frac * math.exp(-_LWI_ALPHA * (temp - td))))


def detect_leaf_wet(
    temp: float, rh: float, dew_point: Optional[float],
    rainfall: float, lwi: float,
) -> bool:
    """Combined leaf-wetness model: Wt=1 if any condition is True."""
    if rainfall > 0:
        return True
    if rh >= _RH_WET_THRESHOLD:
        return True
    if dew_point is not None and (temp - dew_point) <= _DEW_DELTA_WET_MAX:
        return True
    if lwi >= _LWI_WET_THRESHOLD:
        return True
    return False


def calculate_lwd(records: list[dict]) -> dict:
    """Leaf Wetness Duration from hourly records. Each record: temp, rh, dew_point, rainfall, lwi."""
    wet_temps: list[float] = []
    lwi_vals: list[float] = []
    for rec in records:
        lwi = _safe_float(rec.get("lwi")) or 0.0
        lwi_vals.append(lwi)
        if detect_leaf_wet(
            temp=_safe_float(rec.get("temp")) or 0.0,
            rh=_safe_float(rec.get("rh")) or 0.0,
            dew_point=rec.get("dew_point"),
            rainfall=_safe_float(rec.get("rainfall")) or 0.0,
            lwi=lwi,
        ):
            wet_temps.append(_safe_float(rec.get("temp")) or 0.0)

    wet_count = len(wet_temps)
    return {
        "lwd_hours": wet_count,
        "wet_hour_count": wet_count,
        "total_hours": len(records),
        "avg_temperature_wet_hours": round(sum(wet_temps) / wet_count, 2) if wet_temps else 0.0,
        "max_lwi": round(max(lwi_vals), 3) if lwi_vals else 0.0,
        "avg_lwi": round(sum(lwi_vals) / len(lwi_vals), 3) if lwi_vals else 0.0,
    }


def calculate_asri(lwd_hours: float, avg_temp_wet: float) -> float:
    """ASRI = LWD / (a * exp(-b * T)); 0 when no wet hours."""
    if lwd_hours <= 0:
        return 0.0
    denom = _ASRI_A * math.exp(-_ASRI_B * avg_temp_wet)
    return round(lwd_hours / denom, 4) if denom > 0 else 0.0


def classify_risk(asri: float) -> str:
    if asri > 1.5:
        return "SEVERE"
    if asri > 1.0:
        return "HIGH"
    if asri >= 0.5:
        return "MODERATE"
    return "LOW"


def _run_apple_scab_guardrail(weather: Optional[dict]) -> dict:
    _unknown = {
        "enabled": True, "risk_level": "UNKNOWN", "asri": None,
        "lwd_hours": None, "avg_temperature_wet_hours": None,
        "max_lwi": None, "avg_lwi": None, "wet_hour_count": None,
        "total_hours": None,
        "method": "LWI + Combined Wetness + LWD + ASRI",
        "explainability": ["Weather data not available — cannot compute apple scab risk."],
        "confidence": "LOW",
    }
    if not weather:
        return _unknown

    try:
        temps = _hourly(weather, "temperature_2m", "temperature", "temp")
        rhs = _hourly(weather, "relative_humidity_2m", "relative_humidity", "humidity", "rh")
        dew_pts = _hourly(weather, "dew_point_2m", "dew_point", "dewpoint")
        rainfalls = _hourly(weather, "precipitation", "rainfall", "rain")

        if not temps or not rhs:
            return {**_unknown, "explainability": ["Temperature or RH data missing."]}

        dp_available = dew_pts is not None
        n = min(len(temps), len(rhs),
                len(dew_pts) if dp_available else 9999,
                len(rainfalls) if rainfalls else 9999)

        records = []
        for i in range(n):
            t = _safe_float(temps[i]) or 0.0
            rh = _safe_float(rhs[i]) or 0.0
            dp = _safe_float(dew_pts[i]) if dp_available else None
            rain = (_safe_float(rainfalls[i]) or 0.0) if rainfalls else 0.0
            records.append({"temp": t, "rh": rh, "dew_point": dp,
                             "rainfall": rain, "lwi": calculate_lwi(t, rh, dp)})

        stats = calculate_lwd(records)
        asri = calculate_asri(stats["lwd_hours"], stats["avg_temperature_wet_hours"])
        risk = classify_risk(asri)

        notes = [
            "Leaf wetness detected using rainfall, RH, dew point and LWI",
            "LWD calculated as total wet hours",
            "ASRI computed using LWD and temperature",
        ]
        if not dp_available:
            notes.append("Dew point not available — estimated from RH (reduced confidence)")

        return {
            "enabled": True,
            "risk_level": risk,
            "asri": asri,
            "lwd_hours": stats["lwd_hours"],
            "avg_temperature_wet_hours": stats["avg_temperature_wet_hours"],
            "max_lwi": stats["max_lwi"],
            "avg_lwi": stats["avg_lwi"],
            "wet_hour_count": stats["wet_hour_count"],
            "total_hours": stats["total_hours"],
            "method": "LWI + Combined Wetness + LWD + ASRI",
            "explainability": notes,
            "confidence": "HIGH" if dp_available else "MEDIUM",
        }
    except Exception:
        log.warning("apple_scab_guardrail error", exc_info=True)
        return _unknown


def _apple_scab_advisory(risk: str) -> dict:
    _map = {
        "LOW": {
            "risk_level": "LOW", "action_required": False, "timing": "NONE",
            "message": "Apple scab risk is low. No immediate action required.",
            "reason": "ASRI below 0.5 — insufficient wetness duration.",
            "notes": ["Continue monitoring weather conditions."],
        },
        "MODERATE": {
            "risk_level": "MODERATE", "action_required": True,
            "timing": "BEFORE_NEXT_RAIN",
            "message": "Moderate apple scab risk. Apply preventive treatment before next wet event.",
            "reason": "ASRI between 0.5–1.0 — wetness conditions are building.",
            "notes": ["Apply preventive treatment before next rain.", "Follow local horticulture POP."],
        },
        "HIGH": {
            "risk_level": "HIGH", "action_required": True,
            "timing": "BEFORE_NEXT_RAIN",
            "message": "High apple scab risk. Immediate action recommended before next rainfall.",
            "reason": "ASRI above 1.0 — extended leaf wetness, high infection pressure.",
            "notes": ["Apply protective treatment before next rain.", "Follow local horticulture POP."],
        },
        "SEVERE": {
            "risk_level": "SEVERE", "action_required": True, "timing": "URGENT",
            "message": "URGENT: Severe apple scab risk. Immediate spray required.",
            "reason": "ASRI above 1.5 — severe wetness, very high infection pressure.",
            "notes": ["Apply spray immediately.", "Follow local horticulture POP.", "Do not delay."],
        },
        "UNKNOWN": {
            "risk_level": "UNKNOWN", "action_required": False, "timing": "NONE",
            "message": "Apple scab risk could not be determined — weather data missing.",
            "reason": "Insufficient weather data for ASRI calculation.",
            "notes": ["Ensure weather data is available for accurate risk assessment."],
        },
    }
    return _map.get(risk, _map["UNKNOWN"])


# ─── 1b. LAI / BIOMASS SCAB MODIFIER (additive, non-breaking) ────────────────
# Augments the apple_scab_guardrail above with a canopy-density (LAI) modifier.
# Does NOT modify ASRI, LWD, LWI, or the base risk_level. Adds a separate
# `lai_biomass_scab_guardrail` block + a flat `apple_scab_final` summary so the
# UI can always display apple-scab status regardless of base risk.

_LAI_LOW_MAX: float = 2.0
_LAI_HIGH_MIN: float = 4.0

_BASE_CONFIDENCE_NUMERIC = {"HIGH": 90, "MEDIUM": 70, "LOW": 40}


def _extract_lai(
    context_extra: Optional[dict],
    e41_details: Optional[dict],
) -> Any:
    """Pull LAI from explicit input, satellite enrichment, or NDVI proxy.
    Returns a float or the string 'UNKNOWN'. Never raises."""
    candidates: list[Any] = []
    try:
        ex = context_extra or {}
        for key in ("lai", "LAI", "leaf_area_index"):
            if ex.get(key) is not None:
                candidates.append(ex.get(key))
        sat_ex = ex.get("satellite") if isinstance(ex.get("satellite"), dict) else None
        if sat_ex:
            for key in ("lai", "LAI", "leaf_area_index", "lai_current"):
                if sat_ex.get(key) is not None:
                    candidates.append(sat_ex.get(key))

        det = e41_details or {}
        for key in ("satellite", "_satellite_debug", "satellite_debug"):
            blk = det.get(key)
            if isinstance(blk, dict):
                for lkey in ("lai", "LAI", "leaf_area_index", "lai_current"):
                    if blk.get(lkey) is not None:
                        candidates.append(blk.get(lkey))

        for c in candidates:
            f = _safe_float(c)
            if f is not None:
                return f

        # NDVI-derived proxy (Beer–Lambert style): LAI ≈ -ln(1 - NDVI) / k, k≈0.5.
        ndvi_val = None
        for src in (context_extra or {}, (context_extra or {}).get("satellite") or {},
                    (e41_details or {}).get("satellite") or {},
                    (e41_details or {}).get("_satellite_debug") or {}):
            if isinstance(src, dict):
                for nkey in ("ndvi", "NDVI", "ndvi_mean", "ndvi_avg"):
                    if src.get(nkey) is not None:
                        ndvi_val = _safe_float(src.get(nkey))
                        if ndvi_val is not None:
                            break
                if ndvi_val is not None:
                    break
        if ndvi_val is not None and 0.0 < ndvi_val < 1.0:
            try:
                return round(-math.log(1.0 - ndvi_val) / 0.5, 2)
            except (ValueError, ZeroDivisionError):
                pass
    except Exception:
        log.warning("lai_extract_failed", exc_info=True)
    return "UNKNOWN"


def _classify_canopy_density(lai_value: Any) -> str:
    f = _safe_float(lai_value)
    if f is None:
        return "UNKNOWN"
    if f < _LAI_LOW_MAX:
        return "LOW"
    if f < _LAI_HIGH_MIN:
        return "MEDIUM"
    return "HIGH"


def _lai_modifier_for(canopy_density: str) -> tuple[float, str, int]:
    """Return (scab_modifier, effect_on_risk, confidence_delta)."""
    if canopy_density == "LOW":
        return 0.9, "SLIGHTLY_REDUCED", -5
    if canopy_density == "HIGH":
        return 1.3, "INCREASED", 10
    if canopy_density == "MEDIUM":
        return 1.0, "NO_CHANGE", 0
    return 1.0, "UNKNOWN", 0


def _apply_canopy_to_risk(base_risk: str, canopy_density: str) -> str:
    """Bump base risk one step when canopy is HIGH; otherwise pass through."""
    if canopy_density != "HIGH":
        return base_risk
    return {
        "LOW": "MODERATE",
        "MODERATE": "HIGH",
        "HIGH": "VERY_HIGH",
    }.get(base_risk, base_risk)


def _wetness_status_from(scab_g: dict) -> str:
    lwd = scab_g.get("lwd_hours")
    if lwd is None:
        return "UNKNOWN"
    try:
        lwd_f = float(lwd)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if lwd_f <= 0:
        return "DRY"
    if lwd_f < 6:
        return "BRIEFLY_WET"
    if lwd_f < 12:
        return "EXTENDED_WET"
    return "PROLONGED_WET"


def apply_lai_scab_guardrail(
    scab_g: dict,
    context_extra: Optional[dict],
    e41_details: Optional[dict],
) -> tuple[dict, dict]:
    """
    Build the LAI biomass-scab modifier block and the always-on
    `apple_scab_final` summary.

    Returns (lai_block, apple_scab_final). Never raises; both blocks are
    populated even when LAI or weather is missing so the UI can always
    show apple-scab status.
    """
    base_risk = (scab_g or {}).get("risk_level", "UNKNOWN")
    base_conf_str = (scab_g or {}).get("confidence", "LOW")
    base_conf_num = _BASE_CONFIDENCE_NUMERIC.get(str(base_conf_str).upper(), 40)

    try:
        lai_value = _extract_lai(context_extra, e41_details)
        canopy = _classify_canopy_density(lai_value)
        modifier, effect, conf_delta = _lai_modifier_for(canopy)
        adjusted = _apply_canopy_to_risk(base_risk, canopy)

        final_confidence = max(0, min(100, base_conf_num + conf_delta))

        lai_block = {
            "enabled": True,
            "lai_value": lai_value if isinstance(lai_value, (int, float)) else "UNKNOWN",
            "canopy_density": canopy,
            "scab_modifier": modifier,
            "effect_on_risk": effect,
            "base_scab_risk": base_risk,
            "adjusted_scab_risk": adjusted,
            "confidence_adjustment": (
                f"+{conf_delta}" if conf_delta > 0 else str(conf_delta)
            ),
            "reason": (
                "Dense canopy increases humidity retention and slows drying, "
                "increasing scab risk."
                if canopy == "HIGH"
                else "Sparse canopy reduces trapped humidity — scab risk slightly reduced."
                if canopy == "LOW"
                else "Canopy density is moderate — no change to base scab risk."
                if canopy == "MEDIUM"
                else "LAI not available — no canopy-based modification applied."
            ),
            "explainability": [
                "High LAI indicates dense canopy",
                "Dense canopy reduces airflow",
                "Reduced airflow increases leaf wetness duration",
                "Higher wetness duration increases apple scab infection risk",
            ],
        }

        apple_scab_final = {
            "base_risk": base_risk,
            "adjusted_risk": adjusted,
            "lwd_hours": (scab_g or {}).get("lwd_hours"),
            "wetness_status": _wetness_status_from(scab_g or {}),
            "lai_value": lai_block["lai_value"],
            "canopy_density": canopy,
            "lai_effect": effect,
            "final_confidence": final_confidence,
            "advisory_flag": True,
            "advisory": _final_scab_message(adjusted),
        }
        return lai_block, apple_scab_final
    except Exception:
        log.warning("lai_scab_guardrail error", exc_info=True)
        # Graceful fallback — base risk only, advisory still always present.
        return (
            {
                "enabled": True,
                "lai_value": "UNKNOWN",
                "canopy_density": "UNKNOWN",
                "scab_modifier": 1.0,
                "effect_on_risk": "UNKNOWN",
                "base_scab_risk": base_risk,
                "adjusted_scab_risk": base_risk,
                "confidence_adjustment": "0",
                "reason": "LAI guardrail failed — falling back to base risk.",
                "explainability": [
                    "LAI biomass modifier could not be computed.",
                    "Base apple scab risk is preserved.",
                ],
            },
            {
                "base_risk": base_risk,
                "adjusted_risk": base_risk,
                "lwd_hours": (scab_g or {}).get("lwd_hours"),
                "wetness_status": _wetness_status_from(scab_g or {}),
                "lai_value": "UNKNOWN",
                "canopy_density": "UNKNOWN",
                "lai_effect": "UNKNOWN",
                "final_confidence": base_conf_num,
                "advisory_flag": True,
                "advisory": _final_scab_message(base_risk),
            },
        )


# Risk level → farmer-readable percentage band. Used by every E4.1 caller
# (demo /engine/pest-risk AND production /farm-advisory) so the visible
# summary is identical regardless of surface.
_SCAB_RISK_PCT_BAND: dict[str, tuple[int, int]] = {
    "LOW":       (10, 20),
    "MODERATE":  (35, 50),
    "HIGH":      (60, 75),
    "VERY_HIGH": (75, 85),
    "SEVERE":    (85, 95),
}


_CANOPY_WORD = {"LOW": "sparse", "MEDIUM": "moderate", "HIGH": "dense", "UNKNOWN": "unknown"}


def _scab_action_for(risk: str) -> str:
    return {
        "LOW": "Continue routine monitoring; no spray needed.",
        "MODERATE": "Apply preventive scab cover spray before the next wet event.",
        "HIGH": "Apply protective fungicide before the next rainfall.",
        "VERY_HIGH": "Spray immediately and re-inspect within 7 days.",
        "SEVERE": "Spray immediately; do not delay — infection pressure is severe.",
        "UNKNOWN": "Insufficient data — continue routine monitoring until weather data is available.",
    }.get(risk, "Continue routine monitoring.")


def build_farmer_friendly_scab_summary(
    risk: str,
    weather: Optional[dict],
    canopy: str,
    lai_value: Any = None,
    lwd_hours: Any = None,
) -> str:
    """Three-line apple-scab summary for the main advisory.

    Format:
      Apple Scab Risk: X%
      Reason: LAI of <v> indicates <density> canopy. Combined with <weather>,
              leaf surfaces are likely wet for <duration>, which <does/does not>
              meet the infection threshold.
      Action: <recommendation>

    Always emitted (every advisory) regardless of the user's query. Pure
    post-processing — no LLM call. Used by every E4.1 caller so /farm-advisory
    and /farm/pest-risk emit the same block.
    """
    summary = (weather or {}).get("summary") or {}
    rh = summary.get("humidity_pct")
    temp = summary.get("temperature_c")
    # Prefer the guardrail's full-rule LWD (rainfall + RH≥90 + dew-point gap +
    # LWI). Fall back to Open-Meteo's RH-only conducive_duration_hrs when the
    # guardrail value is not yet available (e.g. weather missing).
    dur_hrs = lwd_hours if lwd_hours is not None else summary.get("conducive_duration_hrs")

    band = _SCAB_RISK_PCT_BAND.get(risk)
    pct_text = (
        f"{band[0]}–{band[1]}%" if band is not None else "indeterminate (data missing)"
    )

    canopy_word = _CANOPY_WORD.get(canopy, "unknown")
    lai_text = (
        f"{lai_value:.2f}" if isinstance(lai_value, (int, float))
        else str(lai_value) if lai_value not in (None, "")
        else "unknown"
    )

    weather_bits: list[str] = []
    if isinstance(temp, (int, float)):
        weather_bits.append(f"temperature {int(temp)}°C")
    if isinstance(rh, (int, float)):
        weather_bits.append(f"humidity {int(rh)}%")
    weather_ctx = ", ".join(weather_bits) if weather_bits else "current weather"

    if isinstance(dur_hrs, (int, float)) and dur_hrs > 0:
        wet_text = f"~{int(dur_hrs)} h"
    elif isinstance(dur_hrs, (int, float)) and dur_hrs == 0:
        wet_text = "0 h (canopy is essentially dry)"
    else:
        wet_text = "an unknown duration"

    # Mills-table threshold: ≥9 h leaf-wetness + 10–24°C → infection.
    meets_threshold = False
    if isinstance(dur_hrs, (int, float)) and dur_hrs >= 9:
        if isinstance(temp, (int, float)) and 10 <= temp <= 24:
            meets_threshold = True
        elif temp is None:
            meets_threshold = True  # duration alone is enough to flag
    # High canopy density independently raises micro-climate humidity.
    if canopy == "HIGH":
        meets_threshold = True

    threshold_clause = (
        "meets the infection threshold"
        if meets_threshold
        else "does not meet the infection threshold"
    )

    line1 = f"Apple Scab Risk: {pct_text}"
    line2 = (
        f"Reason: LAI of {lai_text} indicates {canopy_word} canopy. "
        f"Combined with {weather_ctx}, leaf surfaces are likely wet for "
        f"{wet_text}, which {threshold_clause}."
    )
    line3 = f"Action: {_scab_action_for(risk)}"
    return f"{line1}\n{line2}\n{line3}"


def _final_scab_message(risk: str) -> dict:
    msgs = {
        "LOW": "Low risk of apple scab, continue monitoring.",
        "MODERATE": "Moderate risk of apple scab, preventive measures recommended.",
        "HIGH": "High risk of apple scab, protective action advised.",
        "VERY_HIGH": "Very high risk of apple scab, immediate attention required.",
        "SEVERE": "Severe risk of apple scab, immediate spray required.",
        "UNKNOWN": "Apple scab risk could not be determined — weather/LAI data missing.",
    }
    return {
        "risk_level": risk,
        "message": msgs.get(risk, msgs["UNKNOWN"]),
        "always_visible": True,
    }


# ─── 2. RAIN AFTER SPRAY GUARDRAIL ───────────────────────────────────────────

def _forecast_has_rain(weather: dict, hours: int, threshold_mm: float) -> Optional[bool]:
    """None if data missing, else True/False."""
    rainfalls = _hourly(weather, "precipitation", "rainfall", "rain")
    if not rainfalls:
        return None
    return any((_safe_float(r) or 0.0) >= threshold_mm for r in rainfalls[:hours])


def _run_rain_after_spray(weather: Optional[dict], spray_recommended: bool) -> dict:
    base = {"enabled": True, "confidence": "HIGH"}
    if not weather:
        return {**base, "spray_safety_status": "UNKNOWN",
                "forecast_rain_within_12h": None, "action": "UNKNOWN",
                "reason": "Weather data not available.", "confidence": "LOW"}
    try:
        rain = _forecast_has_rain(weather, _RAIN_WASH_HOURS, _RAIN_WASH_MM)
        if rain is None:
            return {**base, "spray_safety_status": "UNKNOWN",
                    "forecast_rain_within_12h": None, "action": "UNKNOWN",
                    "reason": "Rainfall forecast data not available.", "confidence": "LOW"}
        if not spray_recommended:
            return {**base, "spray_safety_status": "NOT_APPLICABLE",
                    "forecast_rain_within_12h": rain, "action": "NOT_APPLICABLE",
                    "reason": "Spray not recommended — wash-off check not applicable."}
        if rain:
            return {**base, "spray_safety_status": "UNSAFE_TO_SPRAY",
                    "forecast_rain_within_12h": True, "action": "DELAY_SPRAY",
                    "reason": "Rain expected within 12 hours — spray may wash off."}
        return {**base, "spray_safety_status": "SAFE_TO_SPRAY",
                "forecast_rain_within_12h": False, "action": "PROCEED",
                "reason": "No significant rainfall expected in next 12 hours."}
    except Exception:
        log.warning("rain_after_spray error", exc_info=True)
        return {**base, "spray_safety_status": "UNKNOWN",
                "forecast_rain_within_12h": None, "action": "UNKNOWN",
                "reason": "Error during computation.", "confidence": "LOW"}


def _rain_after_spray_advisory(status: str) -> dict:
    notes = [
        "Spray effectiveness depends on dry period after application.",
        "Rain within 12 hours can wash off spray.",
    ]
    if status == "UNSAFE_TO_SPRAY":
        return {"action_required": True, "timing": "DELAY",
                "message": "Rain expected within 12 hours — delay spray to avoid wash-off.",
                "reason": "Rain is expected within 12 hours which may reduce effectiveness.",
                "notes": ["Delay spraying until a dry window of at least 12 hours is available.", *notes]}
    if status == "SAFE_TO_SPRAY":
        return {"action_required": False, "timing": "SAFE",
                "message": "No rain expected in next 12 hours — spray window is safe.",
                "reason": "No significant rainfall expected in next 12 hours.", "notes": notes}
    return {"action_required": False, "timing": "NONE",
            "message": "Rain wash-off check not applicable or data unavailable.",
            "reason": "No spray recommended or weather data missing.", "notes": notes}


# ─── 3. PRE-RAIN SPRAY GUARDRAIL ─────────────────────────────────────────────

def _run_pre_rain_spray(weather: Optional[dict], spray_recommended: bool) -> dict:
    base = {"enabled": True, "confidence": "HIGH"}
    if not spray_recommended:
        return {**base, "spray_precheck_status": "NOT_APPLICABLE",
                "rain_expected_soon": None, "action": "NOT_APPLICABLE",
                "timing_priority": "NONE",
                "reason": "No spray recommended — pre-rain check not applicable."}
    if not weather:
        return {**base, "spray_precheck_status": "UNKNOWN", "rain_expected_soon": None,
                "action": "UNKNOWN", "timing_priority": "UNKNOWN",
                "reason": "Weather data not available.", "confidence": "LOW"}
    try:
        rain_mm = _forecast_has_rain(weather, _PRE_RAIN_HOURS, _PRE_RAIN_MM)
        # Check probability (may be 0-1 or 0-100 scale)
        prob_list = _hourly(weather, "precipitation_probability", "rain_probability", "pop")
        rain_prob_high = False
        if prob_list:
            for p in prob_list[:_PRE_RAIN_HOURS]:
                v = _safe_float(p) or 0.0
                threshold = _PRE_RAIN_PROB * 100 if v > 1.0 else _PRE_RAIN_PROB
                if v >= threshold:
                    rain_prob_high = True
                    break

        if rain_mm is None:
            return {**base, "spray_precheck_status": "UNKNOWN", "rain_expected_soon": None,
                    "action": "UNKNOWN", "timing_priority": "UNKNOWN",
                    "reason": "Rainfall forecast data not available.", "confidence": "LOW"}

        rain_expected = bool(rain_mm) or rain_prob_high
        if rain_expected:
            return {**base, "spray_precheck_status": "BLOCKED_DUE_TO_RAIN",
                    "rain_expected_soon": True, "action": "DO_NOT_SPRAY_NOW",
                    "timing_priority": "DELAY",
                    "reason": "Rain is expected soon. Spray may wash off before it becomes effective."}
        return {**base, "spray_precheck_status": "SAFE", "rain_expected_soon": False,
                "action": "SPRAY_ALLOWED", "timing_priority": "SAFE",
                "reason": "No significant rainfall expected soon."}
    except Exception:
        log.warning("pre_rain_spray error", exc_info=True)
        return {**base, "spray_precheck_status": "UNKNOWN", "rain_expected_soon": None,
                "action": "UNKNOWN", "timing_priority": "UNKNOWN",
                "reason": "Error during computation.", "confidence": "LOW"}


def _pre_rain_spray_advisory(status: str) -> dict:
    notes = [
        "Rainfall shortly after spraying reduces effectiveness.",
        "Wait for a dry weather window before spraying.",
    ]
    if status == "BLOCKED_DUE_TO_RAIN":
        return {"action_required": True, "timing": "DELAY",
                "message": "Spray not recommended now because rain may wash off chemical. Wait for a dry window.",
                "reason": "Rain is expected soon. Spray may wash off before becoming effective.",
                "notes": notes}
    if status == "SAFE":
        return {"action_required": False, "timing": "SAFE",
                "message": "No significant rain expected — spray window is safe.",
                "reason": "No significant rainfall expected soon.", "notes": notes}
    return {"action_required": False, "timing": "NONE",
            "message": "Pre-rain spray check not applicable or data unavailable.",
            "reason": "No spray recommended or weather data missing.", "notes": notes}


# ─── 4. WIND SPRAY DRIFT GUARDRAIL ───────────────────────────────────────────

def _run_wind_spray(weather: Optional[dict], spray_recommended: bool) -> dict:
    expl = [
        "Wind guardrail checks forecast wind speed before spray.",
        "High wind can cause spray drift and poor tree coverage.",
        "Spray timing is delayed if wind is above safe threshold.",
    ]
    base = {"enabled": True, "wind_window_hours": _WIND_WINDOW_HOURS,
            "explainability": expl}

    if not spray_recommended:
        return {**base, "wind_status": "SAFE", "max_wind_kmph_next_6h": None,
                "action": "NOT_APPLICABLE", "timing_priority": "NONE",
                "reason": "No spray recommended — wind check not applicable.",
                "confidence": "HIGH", "missing_fields": []}
    if not weather:
        return {**base, "wind_status": "UNKNOWN", "max_wind_kmph_next_6h": None,
                "action": "UNKNOWN", "timing_priority": "UNKNOWN",
                "reason": "Weather data not available.",
                "confidence": "LOW", "missing_fields": ["wind_speed"]}
    try:
        wind_list = _hourly(weather,
            "windspeed_10m", "wind_speed_10m", "wind_speed",
            "windspeed", "wind_speed_kmph", "wind_kmph")
        if not wind_list:
            return {**base, "wind_status": "UNKNOWN", "max_wind_kmph_next_6h": None,
                    "action": "UNKNOWN", "timing_priority": "UNKNOWN",
                    "reason": "Wind speed data not found in forecast.",
                    "confidence": "LOW", "missing_fields": ["wind_speed"]}

        max_wind = max((_safe_float(w) or 0.0) for w in wind_list[:_WIND_WINDOW_HOURS])
        max_wind = round(max_wind, 2)

        if max_wind > _WIND_HIGH_KMPH:
            status, action, priority = "DO_NOT_SPRAY", "DO_NOT_SPRAY_NOW", "DELAY"
            reason = "Very high wind expected. Spray may drift and coverage will be poor."
        elif max_wind >= _WIND_CAUTION_KMPH:
            status, action, priority = "AVOID_SPRAY", "DELAY_SPRAY", "DELAY"
            reason = "High wind may cause spray drift, chemical loss, and uneven disease control."
        elif max_wind >= _WIND_SAFE_KMPH:
            status, action, priority = "CAUTION", "SPRAY_WITH_CAUTION", "CAUTION"
            reason = "Moderate wind expected. Spray only if coverage can be maintained."
        else:
            status, action, priority = "SAFE", "SPRAY_ALLOWED", "SAFE"
            reason = "Wind speed is within safe range for spraying."

        return {**base, "wind_status": status, "max_wind_kmph_next_6h": max_wind,
                "action": action, "timing_priority": priority, "reason": reason,
                "confidence": "HIGH", "missing_fields": []}
    except Exception:
        log.warning("wind_spray error", exc_info=True)
        return {**base, "wind_status": "UNKNOWN", "max_wind_kmph_next_6h": None,
                "action": "UNKNOWN", "timing_priority": "UNKNOWN",
                "reason": "Error during wind computation.",
                "confidence": "LOW", "missing_fields": ["wind_speed"]}


def _wind_spray_advisory(wind_status: str) -> dict:
    notes = [
        "High wind can carry spray away from the tree.",
        "Poor spray coverage can reduce disease control.",
        "Avoid spraying during high wind conditions.",
    ]
    if wind_status in ("DO_NOT_SPRAY", "AVOID_SPRAY"):
        return {"action_required": True, "timing": "DELAY",
                "message": "High wind conditions — delay spray to avoid drift and poor coverage.",
                "reason": "Wind speed exceeds safe threshold for spray application.", "notes": notes}
    if wind_status == "CAUTION":
        return {"action_required": True, "timing": "CAUTION",
                "message": "Moderate wind — spray with caution if coverage can be maintained.",
                "reason": "Wind is in caution range.", "notes": notes}
    if wind_status == "SAFE":
        return {"action_required": False, "timing": "SAFE",
                "message": "Wind conditions are safe for spraying.",
                "reason": "Wind speed is within safe range.", "notes": notes}
    return {"action_required": False, "timing": "UNKNOWN",
            "message": "Wind spray check could not be completed.",
            "reason": "Wind data not available or spray not recommended.", "notes": notes}


# ─── 5. SCAB-PRONE INTERVAL GUARDRAIL ────────────────────────────────────────

def is_primary_scab_stage(stage: Optional[str]) -> Union[bool, str]:
    """True = primary scab period, False = clearly not, 'UNKNOWN' = indeterminate."""
    if not stage:
        return "UNKNOWN"
    sl = stage.lower().replace("-", " ").replace("_", " ")
    for kw in _PRIMARY_SCAB_KW:
        if kw in sl:
            return True
    for kw in _LATE_STAGE_KW:
        if kw in sl:
            return False
    return "UNKNOWN"


def _run_scab_prone_interval(
    extra: Optional[dict],
    current_date: _date,
    spray_recommended: bool,
) -> dict:
    ex = extra or {}
    missing: list[str] = []
    base = {
        "enabled": True,
        "minimum_interval_days": _MIN_SPRAY_INTERVAL,
        "ideal_interval_days": f"{_MIN_SPRAY_INTERVAL}-{_MAX_SPRAY_INTERVAL}",
        "explainability": [
            "This guardrail checks spray interval for apple scab in scab-prone areas.",
            "It prevents repeat apple scab spray before 12 days during primary scab period.",
            "It uses existing project stage names and spray history where available.",
        ],
    }

    disease_zone = (ex.get("disease_zone") or ex.get("disease_risk_zone")
                    or ex.get("risk_zone"))
    if not disease_zone:
        missing.append("disease_zone")

    current_stage = (ex.get("current_stage") or ex.get("crop_stage")
                     or ex.get("stage_name") or ex.get("phenology_stage"))
    if not current_stage:
        missing.append("current_stage")

    last_spray_raw = (ex.get("last_scab_spray_date") or ex.get("last_apple_scab_spray_date")
                      or ex.get("last_spray_date"))
    target_disease = ex.get("target_disease") or ex.get("disease_target") or "apple_scab"
    is_scab_target = "scab" in (target_disease or "").lower()

    # Parse last spray date
    last_spray_date: Optional[_date] = None
    if last_spray_raw:
        try:
            if isinstance(last_spray_raw, _date):
                last_spray_date = last_spray_raw
            else:
                last_spray_date = _date.fromisoformat(str(last_spray_raw))
        except (ValueError, TypeError):
            missing.append("last_spray_date_parse_error")

    zone_prone = (disease_zone or "").lower().replace(" ", "_") in _SCAB_PRONE_ZONES
    stage_check = is_primary_scab_stage(current_stage)

    def _stub(status, action, priority, reason, confidence="HIGH"):
        return {
            **base, "status": status, "disease_zone": disease_zone,
            "current_stage": current_stage, "is_primary_scab_stage": stage_check,
            "last_scab_spray_date": str(last_spray_date) if last_spray_date else None,
            "days_since_last_spray": None, "next_allowed_spray_date": None,
            "action": action, "timing_priority": priority, "reason": reason,
            "confidence": confidence, "missing_fields": missing,
        }

    if not spray_recommended or not is_scab_target:
        return _stub("NOT_APPLICABLE", "NO_INTERVAL_CHECK_REQUIRED", "NONE",
                     "Spray not recommended or target disease is not apple scab.")
    if not zone_prone or stage_check is False:
        return _stub("NOT_APPLICABLE", "NO_INTERVAL_CHECK_REQUIRED", "NONE",
                     "Scab-prone interval guardrail not applicable for current zone or stage.")
    if not disease_zone or stage_check == "UNKNOWN":
        return _stub("UNKNOWN", "UNKNOWN", "UNKNOWN",
                     "Cannot determine applicability — disease zone or stage missing.",
                     "LOW")
    if last_spray_date is None:
        return _stub("NO_PREVIOUS_SCAB_SPRAY_FOUND",
                     "SPRAY_ALLOWED_IF_OTHER_GUARDRAILS_PASS", "SAFE",
                     "No previous apple scab spray record — interval blocking not applicable.",
                     "MEDIUM")

    days_since = (current_date - last_spray_date).days
    next_allowed = last_spray_date + timedelta(days=_MIN_SPRAY_INTERVAL)

    if days_since < _MIN_SPRAY_INTERVAL:
        return {
            **base, "status": "BLOCK_REPEAT_SPRAY", "disease_zone": disease_zone,
            "current_stage": current_stage, "is_primary_scab_stage": True,
            "last_scab_spray_date": str(last_spray_date), "days_since_last_spray": days_since,
            "next_allowed_spray_date": str(next_allowed),
            "action": "DO_NOT_REPEAT_SAME_DISEASE_SPRAY_NOW", "timing_priority": "WAIT",
            "reason": (f"In scab-prone areas, 12–14 day interval must be maintained. "
                       f"{_MIN_SPRAY_INTERVAL - days_since} days remaining."),
            "confidence": "HIGH", "missing_fields": missing,
        }
    if days_since <= _MAX_SPRAY_INTERVAL:
        return {
            **base, "status": "SPRAY_ALLOWED_WITHIN_IDEAL_INTERVAL",
            "disease_zone": disease_zone, "current_stage": current_stage,
            "is_primary_scab_stage": True, "last_scab_spray_date": str(last_spray_date),
            "days_since_last_spray": days_since, "next_allowed_spray_date": str(next_allowed),
            "action": "SPRAY_ALLOWED", "timing_priority": "SAFE",
            "reason": "Minimum 12-day interval completed. Spray is within the recommended 12–14 day window.",
            "confidence": "HIGH", "missing_fields": missing,
        }
    return {
        **base, "status": "SPRAY_ALLOWED_INTERVAL_EXCEEDED",
        "disease_zone": disease_zone, "current_stage": current_stage,
        "is_primary_scab_stage": True, "last_scab_spray_date": str(last_spray_date),
        "days_since_last_spray": days_since, "next_allowed_spray_date": str(next_allowed),
        "action": "SPRAY_ALLOWED", "timing_priority": "DUE_OR_OVERDUE",
        "reason": "More than 14 days since last apple scab spray in scab-prone area.",
        "confidence": "HIGH", "missing_fields": missing,
    }


def _scab_interval_advisory(g: dict) -> dict:
    status = g.get("status", "UNKNOWN")
    timing = {
        "BLOCK_REPEAT_SPRAY": "WAIT",
        "SPRAY_ALLOWED_WITHIN_IDEAL_INTERVAL": "SAFE",
        "SPRAY_ALLOWED_INTERVAL_EXCEEDED": "DUE_OR_OVERDUE",
        "NO_PREVIOUS_SCAB_SPRAY_FOUND": "SAFE",
        "NOT_APPLICABLE": "NONE",
        "UNKNOWN": "UNKNOWN",
    }.get(status, "UNKNOWN")
    msgs = {
        "BLOCK_REPEAT_SPRAY": f"Do not repeat scab spray — minimum 12-day interval not reached. Next allowed: {g.get('next_allowed_spray_date', 'N/A')}.",
        "SPRAY_ALLOWED_WITHIN_IDEAL_INTERVAL": "Apple scab spray is within the recommended 12–14 day interval.",
        "SPRAY_ALLOWED_INTERVAL_EXCEEDED": "More than 14 days since last scab spray — spray is due or overdue.",
        "NO_PREVIOUS_SCAB_SPRAY_FOUND": "No previous scab spray record — first spray allowed if other guardrails pass.",
        "NOT_APPLICABLE": "Scab-prone interval check not applicable for current conditions.",
        "UNKNOWN": "Cannot determine scab spray interval — missing data.",
    }
    return {
        "action_required": status == "BLOCK_REPEAT_SPRAY",
        "timing": timing,
        "message": msgs.get(status, "Unknown status."),
        "reason": g.get("reason", ""),
        "next_allowed_spray_date": g.get("next_allowed_spray_date"),
        "notes": [
            "In scab-prone areas, maintain 12–14 day interval between apple scab sprays up to primary scab stage.",
            "Do not repeat the same disease spray before the minimum interval.",
            "This guardrail controls spray timing only — it does not change disease risk calculation.",
        ],
    }


# ─── 6. HAIL DAMAGE GUARDRAIL ─────────────────────────────────────────────────

def _detect_hail(weather: dict) -> Union[bool, str]:
    # Explicit flag
    for key in ("hail_event", "hail_detected", "has_hail", "hail"):
        v = weather.get(key)
        if v is not None:
            if isinstance(v, bool):
                return v
            if isinstance(v, str) and v.lower() in ("true", "yes", "1"):
                return True
            if isinstance(v, (int, float)) and v > 0:
                return True

    # Condition text
    cond = str(weather.get("condition") or weather.get("weather_condition") or "")
    if "hail" in cond.lower():
        return True

    # WMO codes
    result = _wmo_in_list(weather, _HAIL_WMO)
    if result is True:
        return True
    if result is False:
        return False
    return "UNKNOWN"


def _run_hail_guardrail(weather: Optional[dict]) -> tuple[dict, dict]:
    expl = [
        "Hail causes wounds on leaves and fruits.",
        "Wounds increase susceptibility to disease infection.",
        "Immediate protective action is recommended after hail events.",
    ]
    base = {"enabled": True, "explainability": expl}

    if not weather:
        g = {**base, "hail_event_detected": "UNKNOWN", "status": "UNKNOWN",
             "infection_risk_context": "UNKNOWN", "action": "UNKNOWN",
             "timing_priority": "UNKNOWN", "confidence": "LOW",
             "missing_fields": ["weather_data"]}
        return g, {"hail_damage_event": "UNKNOWN"}

    try:
        hail = _detect_hail(weather)
        if hail is True:
            return (
                {**base, "hail_event_detected": True, "status": "HAIL_EVENT_DETECTED",
                 "infection_risk_context": "HIGH_DUE_TO_WOUNDS",
                 "action": "IMMEDIATE_WOUND_PROTECTION", "timing_priority": "URGENT",
                 "confidence": "HIGH", "missing_fields": []},
                {"hail_damage_event": True},
            )
        if hail is False:
            return (
                {**base, "hail_event_detected": False, "status": "NO_HAIL_EVENT",
                 "infection_risk_context": "NORMAL", "action": "NO_SPECIAL_ACTION",
                 "timing_priority": "NONE", "confidence": "HIGH", "missing_fields": []},
                {"hail_damage_event": False},
            )
        return (
            {**base, "hail_event_detected": "UNKNOWN", "status": "UNKNOWN",
             "infection_risk_context": "UNKNOWN", "action": "UNKNOWN",
             "timing_priority": "UNKNOWN", "confidence": "LOW",
             "missing_fields": ["hail_detection_data"]},
            {"hail_damage_event": "UNKNOWN"},
        )
    except Exception:
        log.warning("hail_guardrail error", exc_info=True)
        g = {**base, "hail_event_detected": "UNKNOWN", "status": "UNKNOWN",
             "infection_risk_context": "UNKNOWN", "action": "UNKNOWN",
             "timing_priority": "UNKNOWN", "confidence": "LOW", "missing_fields": []}
        return g, {"hail_damage_event": "UNKNOWN"}


def _hail_advisory(detected: Union[bool, str]) -> dict:
    if detected is True:
        return {
            "action_required": True, "timing": "URGENT",
            "message": "Hail damage detected. Apply wound-protection spray immediately.",
            "reason": "Hail wounds increase infection risk in apple trees.",
            "notes": [
                "Apply protective spray as soon as possible after hail event.",
                "Follow local horticulture POP recommendations.",
                "Avoid delay — infection risk is highest immediately after injury.",
            ],
        }
    return {
        "action_required": False, "timing": "NONE",
        "message": "No hail-related action required.",
        "reason": ("No hail event detected." if detected is False
                   else "Hail event status unknown."),
    }


# ─── 7. SNOW EVENT GUARDRAIL ──────────────────────────────────────────────────

def _detect_snow(weather: dict) -> Union[bool, str]:
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

    # Temperature + precipitation fallback: T <= 2°C AND precip > 0
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

    # If we have temperature data but no snow indicators, return False
    if temps:
        return False

    return "UNKNOWN"


def _post_snow_melt_risk(weather: dict) -> Union[bool, str]:
    """True if temps rising above 0 AND wet conditions after snow."""
    temps = _hourly(weather, "temperature_2m", "temperature", "temp")
    if not temps:
        return "UNKNOWN"

    recent = [_safe_float(t) or -99.0 for t in temps[:12]]
    rising = any(t > 0 for t in recent)

    rhs = _hourly(weather, "relative_humidity_2m", "relative_humidity", "humidity", "rh")
    precip = _hourly(weather, "precipitation", "rainfall", "rain")
    high_rh = rhs and any((_safe_float(r) or 0.0) >= 85 for r in rhs[:12])
    wet = precip and any((_safe_float(p) or 0.0) > 0 for p in precip[:12])

    if rising and (high_rh or wet):
        return True
    if rising:
        return "UNKNOWN"
    return False


def resolve_stage_risk_context(stage: Optional[str]) -> str:
    """Map existing stage name to a risk context category without renaming it."""
    if not stage:
        return "UNKNOWN_STAGE"
    sl = stage.lower().replace("-", " ").replace("_", " ")
    for kw in _LATE_STAGE_KW:
        if kw in sl:
            return "NO_GREEN_TISSUE_OR_INACTIVE"
    active_kw = {
        "green tip", "bud break", "tight cluster", "pink bud",
        "bloom", "flowering", "petal fall", "fruit set", "fruit development",
        "summer", "growth",
    }
    for kw in active_kw:
        if kw in sl:
            return "SUSCEPTIBLE_GREEN_TISSUE"
    return "UNKNOWN_STAGE"


def _run_snow_guardrail(weather: Optional[dict], current_stage: Optional[str]) -> dict:
    expl = [
        "Snow guardrail uses existing weather and stage logic.",
        "Active snow can temporarily reduce fungal activity.",
        "Snow melt may increase disease risk due to wet canopy conditions.",
        "This guardrail does not modify ASRI, LWD, LWI, or apple scab risk calculation.",
    ]
    stage_ctx = resolve_stage_risk_context(current_stage)
    base = {"enabled": True, "current_stage": current_stage,
            "stage_risk_context": stage_ctx, "explainability": expl}

    if not weather:
        return {**base, "snow_event": "UNKNOWN", "status": "UNKNOWN",
                "active_snow_disease_context": "UNKNOWN",
                "post_melt_disease_risk": "UNKNOWN", "spray_status": "UNKNOWN",
                "confidence": "LOW", "missing_fields": ["weather_data"]}
    try:
        snow = _detect_snow(weather)
        if snow is True:
            melt = _post_snow_melt_risk(weather)
            post_melt = ("ELEVATED" if melt is True else
                         "UNKNOWN" if melt == "UNKNOWN" else "MONITOR")
            return {**base, "snow_event": True, "status": "SNOW_EVENT_DETECTED",
                    "active_snow_disease_context": "TEMPORARILY_LOW_ACTIVITY",
                    "post_melt_disease_risk": post_melt, "spray_status": "DELAY_SPRAY",
                    "confidence": "HIGH", "missing_fields": []}
        if snow is False:
            return {**base, "snow_event": False, "status": "NO_SNOW_EVENT",
                    "active_snow_disease_context": "NORMAL",
                    "post_melt_disease_risk": "NONE", "spray_status": "NORMAL",
                    "confidence": "HIGH", "missing_fields": []}
        return {**base, "snow_event": "UNKNOWN", "status": "UNKNOWN",
                "active_snow_disease_context": "UNKNOWN",
                "post_melt_disease_risk": "UNKNOWN", "spray_status": "UNKNOWN",
                "confidence": "LOW", "missing_fields": ["precipitation_type_or_snowfall"]}
    except Exception:
        log.warning("snow_guardrail error", exc_info=True)
        return {**base, "snow_event": "UNKNOWN", "status": "UNKNOWN",
                "active_snow_disease_context": "UNKNOWN",
                "post_melt_disease_risk": "UNKNOWN", "spray_status": "UNKNOWN",
                "confidence": "LOW", "missing_fields": []}


def _snow_advisory(snow_event: Union[bool, str], post_melt_risk: str) -> dict:
    notes = [
        "Avoid spraying during active snow or very cold wet conditions.",
        "After snow melt, monitor for disease risk due to wet canopy conditions.",
        "Use existing local POP/treatment database if protective action is required.",
    ]
    if snow_event is True:
        if post_melt_risk == "ELEVATED":
            return {"action_required": True, "timing": "MONITOR_AFTER_MELT",
                    "spray_recommendation": "MONITOR",
                    "message": "Active snow detected — delay spray. Monitor after melt for elevated disease risk.",
                    "reason": "Snow melt creates wet canopy conditions that may increase infection risk.",
                    "notes": notes}
        return {"action_required": True, "timing": "DELAY",
                "spray_recommendation": "DELAY_SPRAY",
                "message": "Spraying not recommended during active snow or very cold wet conditions.",
                "reason": "Snow/cold wet conditions reduce spray effectiveness.",
                "notes": notes}
    if snow_event is False:
        return {"action_required": False, "timing": "NONE",
                "spray_recommendation": "NO_SNOW_ACTION",
                "message": "No snow-related pest/disease action required.",
                "reason": "No snow event detected.", "notes": notes}
    return {"action_required": False, "timing": "UNKNOWN",
            "spray_recommendation": "UNKNOWN",
            "message": "Snow event status unknown — check weather data.",
            "reason": "Insufficient weather data to determine snow event.", "notes": notes}


# ─── MAIN DECORATOR ───────────────────────────────────────────────────────────

def decorate_with_guardrails(
    e41_result: dict[str, Any],
    e42_result: dict[str, Any],
    weather: Optional[dict],
    context_extra: Optional[dict],
    current_date: _date,
    e1_summary: Optional[str] = None,
) -> None:
    """
    Add all 7 guardrails to E4.1 and E4.2 results in-place.
    Additive only — never overwrites existing keys. Never raises.
    """
    try:
        if not isinstance(e41_result.get("details"), dict):
            e41_result["details"] = {}
        if not isinstance(e42_result.get("details"), dict):
            e42_result["details"] = {}

        ex = context_extra or {}

        # current_stage: prefer explicit extra fields, fall back to E1 summary text
        current_stage = (
            ex.get("current_stage") or ex.get("crop_stage")
            or ex.get("stage_name") or ex.get("phenology_stage")
            or e1_summary
        )

        # ── 1. Apple Scab ──────────────────────────────────────────────────
        scab_g = _run_apple_scab_guardrail(weather)
        e41_result["details"].setdefault("apple_scab_guardrail", scab_g)
        e42_result["details"].setdefault("apple_scab_advisory",
                                          _apple_scab_advisory(scab_g.get("risk_level", "UNKNOWN")))

        # ── 1b. LAI / biomass canopy modifier (additive, non-breaking) ────
        lai_block, apple_scab_final = apply_lai_scab_guardrail(
            scab_g=scab_g,
            context_extra=ex,
            e41_details=e41_result.get("details"),
        )
        e41_result["details"].setdefault("lai_biomass_scab_guardrail", lai_block)
        e41_result["details"].setdefault("apple_scab_final", apple_scab_final)
        e42_result["details"].setdefault("apple_scab_final", apple_scab_final)

        # Prepend the farmer-friendly apple-scab line to E4.1's summary so
        # every caller (production /farm-advisory and demo /engine/pest-risk
        # alike) emits the SAME visible advisory. Pure string mutation —
        # no extra LLM call. Idempotent: skipped if the line is already there.
        try:
            adjusted = (
                apple_scab_final.get("adjusted_risk")
                or apple_scab_final.get("base_risk") or "UNKNOWN"
            )
            scab_line = build_farmer_friendly_scab_summary(
                risk=adjusted,
                weather=weather,
                canopy=apple_scab_final.get("canopy_density") or "UNKNOWN",
                lai_value=apple_scab_final.get("lai_value"),
                lwd_hours=apple_scab_final.get("lwd_hours"),
            )
            existing = (e41_result.get("summary") or "").strip()
            if "Apple Scab Risk" not in existing and "Apple Scab risk" not in existing:
                e41_result["summary"] = (
                    f"{scab_line}\n\n{existing}" if existing else scab_line
                )

            # Surface scab in triggered_organisms so downstream IPM (E4.2)
            # picks it up. Only when adjusted risk is actionable. Substring
            # dedup against existing entries (which may be dicts or strings).
            if adjusted in ("MODERATE", "HIGH", "VERY_HIGH", "SEVERE"):
                triggered = e41_result["details"].get("triggered_organisms")
                if not isinstance(triggered, list):
                    triggered = []
                already = any("scab" in str(
                    (t or {}).get("organism_name") if isinstance(t, dict) else t
                ).lower() for t in triggered)
                if not already:
                    triggered.append("Apple Scab")
                    e41_result["details"]["triggered_organisms"] = triggered
        except Exception:
            log.warning("apple_scab_summary_inject_failed", exc_info=True)

        # Determine spray_recommended for downstream guardrails
        if "spray_recommended" in ex:
            spray_rec = bool(ex["spray_recommended"])
        else:
            spray_rec = scab_g.get("risk_level", "LOW") in ("MODERATE", "HIGH", "SEVERE")

        # ── 2. Rain After Spray ────────────────────────────────────────────
        rain_after = _run_rain_after_spray(weather, spray_rec)
        e41_result["details"].setdefault("rain_after_spray_guardrail", rain_after)
        e42_result["details"].setdefault("rain_after_spray_advisory",
                                          _rain_after_spray_advisory(
                                              rain_after.get("spray_safety_status", "UNKNOWN")))

        # ── 3. Pre-Rain Spray ──────────────────────────────────────────────
        pre_rain = _run_pre_rain_spray(weather, spray_rec)
        e41_result["details"].setdefault("pre_rain_spray_guardrail", pre_rain)
        e42_result["details"].setdefault("pre_rain_spray_advisory",
                                          _pre_rain_spray_advisory(
                                              pre_rain.get("spray_precheck_status", "UNKNOWN")))

        # ── 4. Wind Spray ──────────────────────────────────────────────────
        wind_g = _run_wind_spray(weather, spray_rec)
        e41_result["details"].setdefault("wind_spray_guardrail", wind_g)
        e42_result["details"].setdefault("wind_spray_advisory",
                                          _wind_spray_advisory(
                                              wind_g.get("wind_status", "UNKNOWN")))

        # ── 5. Scab-Prone Interval ─────────────────────────────────────────
        interval_g = _run_scab_prone_interval(ex, current_date, spray_rec)
        e41_result["details"].setdefault("scab_prone_interval_guardrail", interval_g)
        e42_result["details"].setdefault("scab_prone_interval_advisory",
                                          _scab_interval_advisory(interval_g))

        # ── 6. Hail Damage ─────────────────────────────────────────────────
        hail_g, yield_signals = _run_hail_guardrail(weather)
        e41_result["details"].setdefault("hail_damage_guardrail", hail_g)
        e41_result["details"].setdefault("yield_signals", yield_signals)
        e42_result["details"].setdefault("hail_damage_advisory",
                                          _hail_advisory(
                                              hail_g.get("hail_event_detected", "UNKNOWN")))

        # ── 7. Snow Event ──────────────────────────────────────────────────
        snow_g = _run_snow_guardrail(weather, current_stage)
        e41_result["details"].setdefault("snow_pest_risk_guardrail", snow_g)
        snow_adv = _snow_advisory(
            snow_g.get("snow_event", "UNKNOWN"),
            snow_g.get("post_melt_disease_risk", "UNKNOWN"),
        )
        e42_result["details"].setdefault("snow_pest_cure_advisory", snow_adv)

    except Exception:
        log.exception("decorate_with_guardrails: unexpected top-level error")
