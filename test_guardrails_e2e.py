"""
End-to-end guardrail test runner.

Tests every guardrail scenario for E3 (nutrition) and E4 (pest/disease)
through the live FastAPI server at http://localhost:8000.

USAGE
-----
1. Start the server in one terminal:
       python -m uvicorn app.main:app --port 8000

2. Run this script in another terminal (from D:\\agri rag):
       python test_guardrails_e2e.py

OUTPUT
------
Each scenario prints the guardrail key it is testing, the expected outcome,
and the actual value returned by the API so you can verify at a glance.
"""

import json
import sys

import requests

BASE = "http://localhost:8000"
ENG3_URL = f"{BASE}/advisory/eng3"
FULL_URL  = f"{BASE}/advisory"

SOWING = "2026-01-01"
TODAY  = "2026-05-04"

# ── helpers ───────────────────────────────────────────────────────────────────

def post(url: str, body: dict) -> dict:
    r = requests.post(url, json=body, timeout=120)
    if r.status_code != 200:
        return {"_http_error": r.status_code, "_body": r.text[:500]}
    return r.json()


def _ctx(weather=None, extra=None) -> dict:
    """Minimal AdvisoryContext body."""
    return {
        "crop": "apple",
        "sowing_date": SOWING,
        "current_date": TODAY,
        "weather": weather,
        "extra": extra or {},
    }


def _hourly_weather(temp=None, precip=None, wind=None, wmo=None, rh=None, hours=24) -> dict:
    """Build an hourly weather dict the guardrails can parse."""
    h = {}
    if temp   is not None: h["temperature_2m"]        = [temp]   * hours
    if precip is not None: h["precipitation"]         = [precip] * hours
    if wind   is not None: h["windspeed_10m"]         = [wind]   * hours
    if wmo    is not None: h["weathercode"]           = [wmo]    * hours
    if rh     is not None: h["relative_humidity_2m"]  = [rh]     * hours
    return {"hourly": h}


PASS = 0
FAIL = 0

def section(title: str):
    print("\n" + "=" * 72)
    print("  " + title)
    print("=" * 72)


def scenario(title: str):
    print("\n" + "-" * 72)
    print(title)
    print("-" * 72)


def check(label: str, expected: str, actual):
    global PASS, FAIL
    actual_s = str(actual)
    if actual_s == expected:
        PASS += 1
        marker = "[OK  ]"
    else:
        FAIL += 1
        marker = "[FAIL]"
    print(f"  {marker}  {label}")
    print(f"           expected : {expected}")
    print(f"           got      : {actual_s}")


def get_ng(result: dict) -> dict:
    """Extract nutrition_guardrails block from an E3 result."""
    return (result.get("fertilizer") or result).get("details", {}).get("nutrition_guardrails", {})


def get_final(result: dict) -> str:
    return get_ng(result).get("final_nutrition_decision", {}).get("fertilizer_application_status", "MISSING")


def get_e4g(result: dict, key: str) -> dict:
    return result.get("pest_disease_risk", {}).get("details", {}).get(key, {})


# =============================================================================
#  E3 NUTRITION GUARDRAILS
# =============================================================================

section("E3 NUTRITION GUARDRAILS  (endpoint: POST /advisory/eng3)")

# ---------- Scenario 1: Rain -> DELAYED --------------------------------------
scenario("SCENARIO 1 -- Rain Before Fertilizer -> DELAYED")
print("  Input : 2.0 mm/h x 24 h = 48 mm total  (threshold = 10 mm)")

w = _hourly_weather(temp=18.0, precip=2.0, wmo=2)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
rain_g = ng.get("rain_before_fertilizer_guardrail", {})
check("rain_before_fertilizer_guardrail.status",
      "DELAY_FERTILIZER_DUE_TO_RAIN", rain_g.get("status"))
check("rain_before_fertilizer_guardrail.total_rain_next_24h_mm",
      "48.0", rain_g.get("total_rain_next_24h_mm"))
check("final_nutrition_decision.fertilizer_application_status",
      "DELAYED", get_final(r))

# ---------- Scenario 2: Cold -> BLOCKED --------------------------------------
scenario("SCENARIO 2 -- Cold Fertilizer -> BLOCKED")
print("  Input : temp = 2.0 degC  (threshold = 5.0 degC), light rain, no snow")

w = _hourly_weather(temp=2.0, precip=0.1, wmo=2)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
cold_g = ng.get("cold_snow_fertilizer_guardrail", {})
check("cold_snow_fertilizer_guardrail.status",
      "BLOCK_FERTILIZER_DUE_TO_COLD_OR_SNOW", cold_g.get("status"))
check("cold_snow_fertilizer_guardrail.min_temperature_next_24h_c",
      "2.0", cold_g.get("min_temperature_next_24h_c"))
check("final_nutrition_decision.fertilizer_application_status",
      "BLOCKED", get_final(r))

# ---------- Scenario 3: Snow -> BLOCKED via WMO 73 ---------------------------
scenario("SCENARIO 3 -- Snow -> BLOCKED (WMO code 73 = moderate snow)")
print("  Input : WMO 73, temp = 10 degC")

w = _hourly_weather(temp=10.0, precip=0.0, wmo=73)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
cold_g = ng.get("cold_snow_fertilizer_guardrail", {})
check("cold_snow_fertilizer_guardrail.status",
      "BLOCK_FERTILIZER_DUE_TO_COLD_OR_SNOW", cold_g.get("status"))
check("cold_snow_fertilizer_guardrail.snow_event",
      "True", cold_g.get("snow_event"))
check("final_nutrition_decision.fertilizer_application_status",
      "BLOCKED", get_final(r))

# ---------- Scenario 4: Hail -> RECOVERY_MODE via WMO 96 ---------------------
scenario("SCENARIO 4 -- Hail Recovery Nutrition -> RECOVERY_MODE (WMO 96)")
print("  Input : WMO 96 = violent hail, temp = 18 degC, no rain")

w = _hourly_weather(temp=18.0, precip=0.0, wmo=96)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
hail_g = ng.get("hail_recovery_nutrition_guardrail", {})
check("hail_recovery_nutrition_guardrail.status",
      "HAIL_RECOVERY_MODE", hail_g.get("status"))
check("hail_recovery_nutrition_guardrail.hail_event",
      "True", hail_g.get("hail_event"))
check("final_nutrition_decision.fertilizer_application_status",
      "RECOVERY_MODE", get_final(r))

# ---------- Scenario 5: Hail via context.extra flag --------------------------
scenario("SCENARIO 5 -- Hail via context.extra flag -> RECOVERY_MODE")
print("  Input : extra.hail_event = True, normal temp/rain weather")

w = _hourly_weather(temp=18.0, precip=0.1, wmo=2)
r = post(ENG3_URL, _ctx(weather=w, extra={"hail_event": True}))
ng = get_ng(r)
hail_g = ng.get("hail_recovery_nutrition_guardrail", {})
check("hail_recovery_nutrition_guardrail.status",
      "HAIL_RECOVERY_MODE", hail_g.get("status"))
check("final_nutrition_decision.fertilizer_application_status",
      "RECOVERY_MODE", get_final(r))

# ---------- Scenario 6: Frost -> HOLD via explicit flag + warm temp ----------
# Use extra.frost_event = True with warm temp so ONLY frost triggers.
# temp <= 0 degC would also trigger Cold/Snow guardrail (BLOCKED) which beats HOLD.
# To get HOLD as the final winner, frost must trigger without cold triggering.
scenario("SCENARIO 6 -- Frost Damage Hold -> HOLD  (explicit flag, warm temp=8 degC)")
print("  Input : extra.frost_event = True, temp = 8 degC (no cold trigger)")

w = _hourly_weather(temp=8.0, precip=0.1, wmo=2)
r = post(ENG3_URL, _ctx(weather=w, extra={"frost_event": True}))
ng = get_ng(r)
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("frost_damage_hold_guardrail.status",
      "HOLD_FERTILIZER_DUE_TO_FROST", frost_g.get("status"))
check("frost_damage_hold_guardrail.frost_event_recent",
      "True", frost_g.get("frost_event_recent"))
check("frost_damage_hold_guardrail.action",
      "HOLD_FERTILIZER", frost_g.get("action"))
check("frost_damage_hold_guardrail.timing_priority",
      "HOLD", frost_g.get("timing_priority"))
check("frost_damage_hold_guardrail.nutrition_mode",
      "ASSESS_DAMAGE_FIRST", frost_g.get("nutrition_mode"))
check("final_nutrition_decision.fertilizer_application_status",
      "HOLD", get_final(r))

# ---------- Scenario 7: Frost via condition text -----------------------------
scenario("SCENARIO 7 -- Frost via weather condition text -> HOLD")
print("  Input : condition = 'ground frost overnight', temp = 8 degC (above 0)")

w = {"condition": "ground frost overnight",
     "hourly": {"temperature_2m": [8.0] * 24}}
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("frost_damage_hold_guardrail.status",
      "HOLD_FERTILIZER_DUE_TO_FROST", frost_g.get("status"))
check("final_nutrition_decision.fertilizer_application_status",
      "HOLD", get_final(r))

# ---------- Scenario 8: Frost via context.extra flag -------------------------
scenario("SCENARIO 8 -- Frost via context.extra flag -> HOLD")
print("  Input : extra.frost_event = True, warm weather (12 degC)")

w = _hourly_weather(temp=12.0, precip=0.0, wmo=2)
r = post(ENG3_URL, _ctx(weather=w, extra={"frost_event": True}))
ng = get_ng(r)
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("frost_damage_hold_guardrail.status",
      "HOLD_FERTILIZER_DUE_TO_FROST", frost_g.get("status"))
check("final_nutrition_decision.fertilizer_application_status",
      "HOLD", get_final(r))

# ---------- Scenario 9: Priority -- Hail (P1) beats Frost (P3) ---------------
scenario("SCENARIO 9 -- PRIORITY: Hail (RECOVERY_MODE) beats Frost (HOLD)")
print("  Input : WMO 96 (hail) + temp = -2 degC (frost)")

w = _hourly_weather(temp=-2.0, precip=0.0, wmo=96)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
hail_g  = ng.get("hail_recovery_nutrition_guardrail", {})
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("hail_recovery_nutrition_guardrail.status  -> HAIL_RECOVERY_MODE",
      "HAIL_RECOVERY_MODE", hail_g.get("status"))
check("frost_damage_hold_guardrail.status        -> HOLD_FERTILIZER_DUE_TO_FROST",
      "HOLD_FERTILIZER_DUE_TO_FROST", frost_g.get("status"))
check("final decision (RECOVERY_MODE wins over HOLD)",
      "RECOVERY_MODE", get_final(r))

# ---------- Scenario 10: Priority -- Cold/Snow (P2) beats Frost (P3) ---------
scenario("SCENARIO 10 -- PRIORITY: Cold/Snow (BLOCKED) beats Frost (HOLD)")
print("  Input : WMO 73 (snow) + temp = -2 degC")

w = _hourly_weather(temp=-2.0, precip=0.0, wmo=73)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
cold_g  = ng.get("cold_snow_fertilizer_guardrail", {})
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("cold_snow_fertilizer_guardrail.status     -> BLOCK_FERTILIZER_DUE_TO_COLD_OR_SNOW",
      "BLOCK_FERTILIZER_DUE_TO_COLD_OR_SNOW", cold_g.get("status"))
check("frost_damage_hold_guardrail.status        -> HOLD_FERTILIZER_DUE_TO_FROST",
      "HOLD_FERTILIZER_DUE_TO_FROST", frost_g.get("status"))
check("final decision (BLOCKED wins over HOLD)",
      "BLOCKED", get_final(r))

# ---------- Scenario 11: Priority -- Frost (P3) beats Rain (P4) --------------
# Use extra.frost_event = True with warm temp so only frost + rain trigger.
# temp <= 0 degC would trigger Cold (BLOCKED, P2) which beats HOLD (P3).
scenario("SCENARIO 11 -- PRIORITY: Frost (HOLD) beats Rain (DELAYED)")
print("  Input : extra.frost_event=True + temp=8 degC + 2mm/h precip (48mm total)")

w = _hourly_weather(temp=8.0, precip=2.0, wmo=2)
r = post(ENG3_URL, _ctx(weather=w, extra={"frost_event": True}))
ng = get_ng(r)
rain_g  = ng.get("rain_before_fertilizer_guardrail", {})
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("rain_before_fertilizer_guardrail.status   -> DELAY_FERTILIZER_DUE_TO_RAIN",
      "DELAY_FERTILIZER_DUE_TO_RAIN", rain_g.get("status"))
check("frost_damage_hold_guardrail.status        -> HOLD_FERTILIZER_DUE_TO_FROST",
      "HOLD_FERTILIZER_DUE_TO_FROST", frost_g.get("status"))
check("final decision (HOLD wins over DELAYED)",
      "HOLD", get_final(r))

# ---------- Scenario 12: All clear -> ALLOWED ---------------------------------
scenario("SCENARIO 12 -- All guardrails clear -> ALLOWED")
print("  Input : temp = 18 degC, precip = 0.1mm/h (2.4mm total), WMO 2 (clear)")

w = _hourly_weather(temp=18.0, precip=0.1, wmo=2)
r = post(ENG3_URL, _ctx(weather=w))
ng = get_ng(r)
rain_g  = ng.get("rain_before_fertilizer_guardrail", {})
cold_g  = ng.get("cold_snow_fertilizer_guardrail", {})
hail_g  = ng.get("hail_recovery_nutrition_guardrail", {})
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("rain  status -> FERTILIZER_RAIN_SAFE",
      "FERTILIZER_RAIN_SAFE",       rain_g.get("status"))
check("cold  status -> FERTILIZER_TEMPERATURE_SAFE",
      "FERTILIZER_TEMPERATURE_SAFE", cold_g.get("status"))
check("hail  status -> NO_HAIL_RECOVERY_NEEDED",
      "NO_HAIL_RECOVERY_NEEDED",    hail_g.get("status"))
check("frost status -> NO_FROST_RESTRICTION",
      "NO_FROST_RESTRICTION",       frost_g.get("status"))
check("final_nutrition_decision.fertilizer_application_status",
      "ALLOWED", get_final(r))

# ---------- Scenario 13: No weather -> UNKNOWN --------------------------------
scenario("SCENARIO 13 -- No weather data -> UNKNOWN")
print("  Input : weather = null")

r = post(ENG3_URL, _ctx(weather=None))
ng = get_ng(r)
check("final_nutrition_decision.fertilizer_application_status",
      "UNKNOWN", get_final(r))
print(f"    nutrition_guardrails.enabled : {ng.get('enabled')}")

# ---------- Scenario 14: fertilizer_recommended=False -> ALLOWED --------------
scenario("SCENARIO 14 -- fertilizer_recommended=False -> all NOT_APPLICABLE -> ALLOWED")
print("  Input : extra.fertilizer_recommended = false")

w = _hourly_weather(temp=18.0, precip=0.1, wmo=2)
r = post(ENG3_URL, _ctx(weather=w, extra={"fertilizer_recommended": False}))
ng = get_ng(r)
rain_g  = ng.get("rain_before_fertilizer_guardrail", {})
cold_g  = ng.get("cold_snow_fertilizer_guardrail", {})
frost_g = ng.get("frost_damage_hold_guardrail", {})
check("rain  status -> NOT_APPLICABLE", "NOT_APPLICABLE", rain_g.get("status"))
check("cold  status -> NOT_APPLICABLE", "NOT_APPLICABLE", cold_g.get("status"))
check("frost status -> NOT_APPLICABLE", "NOT_APPLICABLE", frost_g.get("status"))
check("final_nutrition_decision.fertilizer_application_status",
      "ALLOWED", get_final(r))


# =============================================================================
#  E4 PEST/DISEASE GUARDRAILS
# =============================================================================

section("E4 PEST/DISEASE GUARDRAILS  (endpoint: POST /advisory  - all engines)")

print()
print("NOTE: Full /advisory runs the LLM. Each request may take 30-90 seconds.")
print()

# ---------- E4 Scenario 1: Hail damage guardrail -----------------------------
scenario("E4 SCENARIO 1 -- Hail Damage Guardrail (WMO 96 = violent hail)")
print("  Input : WMO 96 + high RH 85% + temp 18 degC")

w = {
    "hourly": {
        "temperature_2m":       [18.0] * 48,
        "relative_humidity_2m": [85.0] * 48,
        "precipitation":        [0.2]  * 48,
        "windspeed_10m":        [5.0]  * 48,
        "weathercode":          [96]   * 48,
    }
}
r = post(FULL_URL, _ctx(weather=w))
hail_g = get_e4g(r, "hail_damage_guardrail")
print(f"    hail_damage_guardrail keys: {list(hail_g.keys())}")
check("hail_damage_guardrail.hail_event_detected",
      "True",  hail_g.get("hail_event_detected"))
check("hail_damage_guardrail.action",
      "IMMEDIATE_WOUND_PROTECTION", hail_g.get("action"))

# ---------- E4 Scenario 2: Snow pest risk guardrail --------------------------
scenario("E4 SCENARIO 2 -- Snow Pest Risk Guardrail (WMO 73 = moderate snow)")
print("  Input : WMO 73 for first 24 h, clear after; warm post-melt temp")

w = {
    "hourly": {
        "temperature_2m":       [5.0]  * 24 + [12.0] * 24,
        "relative_humidity_2m": [90.0] * 48,
        "precipitation":        [0.3]  * 48,
        "windspeed_10m":        [4.0]  * 48,
        "weathercode":          [73]   * 24 + [2]    * 24,
    }
}
r = post(FULL_URL, _ctx(weather=w))
snow_g = get_e4g(r, "snow_pest_risk_guardrail")
print(f"    snow_pest_risk_guardrail keys: {list(snow_g.keys())}")
check("snow_pest_risk_guardrail.snow_event",
      "True", snow_g.get("snow_event"))

# ---------- E4 Scenario 3: Wind spray guardrail ------------------------------
# spray_recommended is True only when scab risk >= MODERATE.
# Use RH=95%, temp=18 degC to get SEVERE scab risk (spray_recommended=True),
# THEN the wind check actually runs and classifies 30 km/h as DO_NOT_SPRAY.
scenario("E4 SCENARIO 3 -- Wind Spray Guardrail (30 km/h, above HIGH threshold 25)")
print("  Input : windspeed_10m = 30 km/h + RH=95% (scab risk SEVERE -> spray recommended)")

w = {
    "hourly": {
        "temperature_2m":       [18.0] * 48,
        "relative_humidity_2m": [95.0] * 48,
        "precipitation":        [0.5]  * 48,
        "windspeed_10m":        [30.0] * 48,
        "weathercode":          [63]   * 48,
    }
}
r = post(FULL_URL, _ctx(weather=w))
wind_g = get_e4g(r, "wind_spray_guardrail")
print(f"    wind_spray_guardrail keys: {list(wind_g.keys())}")
print(f"    wind_status              : {wind_g.get('wind_status')}")
print(f"    max_wind_kmph_next_6h    : {wind_g.get('max_wind_kmph_next_6h')}")
check("wind_spray_guardrail.wind_status",
      "DO_NOT_SPRAY", wind_g.get("wind_status"))

# ---------- E4 Scenario 4: Rain after spray ----------------------------------
scenario("E4 SCENARIO 4 -- Rain After Spray Guardrail (wash-off risk)")
print("  Input : precipitation = 1.5 mm/h  (threshold = 0.5 mm in 12 h)")

w = {
    "hourly": {
        "temperature_2m":       [18.0] * 48,
        "relative_humidity_2m": [80.0] * 48,
        "precipitation":        [1.5]  * 48,
        "windspeed_10m":        [5.0]  * 48,
        "weathercode":          [63]   * 48,
    }
}
r = post(FULL_URL, _ctx(weather=w))
rain_after_g = get_e4g(r, "rain_after_spray_guardrail")
print(f"    rain_after_spray_guardrail keys: {list(rain_after_g.keys())}")
check("rain_after_spray_guardrail.spray_safety_status",
      "UNSAFE_TO_SPRAY", rain_after_g.get("spray_safety_status"))

# ---------- E4 Scenario 5: Apple scab ----------------------------------------
scenario("E4 SCENARIO 5 -- Apple Scab Guardrail (high RH + warm = HIGH/SEVERE)")
print("  Input : RH = 95%, temp = 18 degC, sustained light rain for 48 h")

w = {
    "hourly": {
        "temperature_2m":       [18.0] * 48,
        "relative_humidity_2m": [95.0] * 48,
        "precipitation":        [0.5]  * 48,
        "windspeed_10m":        [3.0]  * 48,
        "weathercode":          [63]   * 48,
    }
}
r = post(FULL_URL, _ctx(weather=w))
scab_g = get_e4g(r, "apple_scab_guardrail")
print(f"    apple_scab_guardrail keys : {list(scab_g.keys())}")
print(f"    asri_risk_level           : {scab_g.get('asri_risk_level')}")
print(f"    spray_recommended         : {scab_g.get('spray_recommended')}")
print(f"    lwd_hours                 : {scab_g.get('lwd_hours')}")
print(f"    asri_score                : {scab_g.get('asri_score')}")


# =============================================================================
#  FULL PAYLOAD DUMP
# =============================================================================

section("FULL PAYLOAD DUMP -- All guardrails in one response (all-clear weather)")

print("\nRunning full /advisory with clean weather ...")
w = _hourly_weather(temp=18.0, precip=0.1, wind=5.0, wmo=2, rh=75.0, hours=48)
r = post(FULL_URL, _ctx(weather=w))

print("\n-- E3 nutrition_guardrails block --")
ng = get_ng(r)
print(json.dumps(ng, indent=2, default=str))

print("\n-- E4.1 pest_disease_risk guardrail summary --")
e4d = r.get("pest_disease_risk", {}).get("details", {})
for k in e4d:
    if k.endswith("_guardrail") or k == "yield_signals":
        val = e4d[k]
        status = val.get("status") if isinstance(val, dict) else val
        print(f"  {k}: status = {status}")

print("\n-- E4.2 pest_disease_cure advisory summary --")
e42d = r.get("pest_disease_cure", {}).get("details", {})
for k in e42d:
    if k.endswith("_advisory") or k.endswith("_guardrail"):
        print(f"  {k}: {e42d[k]}")


# =============================================================================
#  SUMMARY
# =============================================================================

total = PASS + FAIL
print("\n" + "=" * 72)
print("  TEST SUMMARY")
print("=" * 72)
print(f"  Passed : {PASS} / {total}")
print(f"  Failed : {FAIL} / {total}")
if FAIL == 0:
    print("  ALL CHECKS PASSED")
else:
    print("  SOME CHECKS FAILED -- review [FAIL] lines above")
print("=" * 72 + "\n")

sys.exit(1 if FAIL > 0 else 0)
