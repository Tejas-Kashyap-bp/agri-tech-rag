"""
Engine 2 — Irrigation parameter fact-retrieval test.

The RAG system must extract stage-specific Kc, MAD, and root_depth_mm from
the ingested 'maize_irrigation_parameters' document. We compare against the
source JSON values (the ground truth — the same values the integrated rule
engine consumes from its config).

Each maize stage S1-S5 is queried once via /advisory/eng2 with weather context
that forces the model to surface stage parameters in its reasoning.
"""
import json
import re
from datetime import date, timedelta

import requests

RAG = "http://localhost:8765/advisory/eng2"
CURRENT = date(2026, 4, 29)

# Source-of-truth values from /dummy_data/maize/maize_irrigation_parameters.json
GROUND_TRUTH = {
    "S1": {"kc": 0.4, "mad": 0.5, "root_depth_mm": 300},
    "S2": {"kc": 0.8, "mad": 0.4, "root_depth_mm": 600},
    "S3": {"kc": 1.2, "mad": 0.3, "root_depth_mm": 1000},
    "S4": {"kc": 1.0, "mad": 0.4, "root_depth_mm": 1200},
    "S5": {"kc": 0.6, "mad": 0.6, "root_depth_mm": 1200},
}

# DAS that lands in the middle of each maize stage.
STAGE_DAS = {"S1": 7, "S2": 25, "S3": 50, "S4": 80, "S5": 105}


def call_rag(das: int) -> dict:
    sowing = (CURRENT - timedelta(days=das)).isoformat()
    body = {
        "crop": "maize",
        "sowing_date": sowing,
        "current_date": CURRENT.isoformat(),
        "weather": {
            "temperature_c": 32,
            "humidity_pct": 40,
            "wind_mps": 3,
            "rainfall_forecast_mm": 0,
            "et0_last_7_days": [5.0, 4.8, 5.2, 5.0, 4.7, 5.1, 4.9],
            "rain_last_7_days": 0,
        },
    }
    r = requests.post(RAG, json=body, timeout=120)
    r.raise_for_status()
    return r.json()


def _walk_numeric_keys(obj, key_pattern):
    """Yield numeric values whose containing key matches key_pattern (case-insens)."""
    pat = re.compile(key_pattern, re.IGNORECASE)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if pat.search(k) and isinstance(v, (int, float)):
                yield v
            yield from _walk_numeric_keys(v, key_pattern)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk_numeric_keys(x, key_pattern)


def extract_params(j: dict) -> dict:
    """Pull kc/mad/root_depth out of the (possibly variable) RAG schema.
    Gemini is creative with key names — search ANY nested key whose name contains
    'kc'/'mad'/'root_depth'.
    """
    irr = j.get("irrigation", {}) or {}
    details = irr.get("details", {}) or {}

    out = {}
    # kc: numeric in [0.1, 2.0]
    for v in _walk_numeric_keys(details, r"\bkc\b|crop_coefficient"):
        if 0.1 <= v <= 2.0:
            out["kc"] = float(v)
            break
    # mad: numeric in (0, 1)
    for v in _walk_numeric_keys(details, r"\bmad\b|allowable_depletion"):
        f = float(v)
        f = f / 100 if f > 1 else f
        if 0 < f < 1:
            out["mad"] = f
            break
    # root depth: numeric in [100, 3000] mm
    for v in _walk_numeric_keys(details, r"root[_ ]?depth"):
        if 100 <= v <= 3000:
            out["root_depth_mm"] = int(v)
            break

    # Last-resort regex over the reasoning prose.
    text = json.dumps(details)
    if "kc" not in out:
        m = re.search(r"[Kk]c\s*(?:is|=|of|:)\s*(\d+\.?\d*)", text)
        if m:
            out["kc"] = float(m.group(1))
    if "mad" not in out:
        m = re.search(r"MAD[^0-9]*(\d+\.?\d*)", text)
        if m:
            v = float(m.group(1))
            out["mad"] = v / 100 if v > 1 else v
    if "root_depth_mm" not in out:
        m = re.search(r"root[_ ]depth[^0-9]*(\d+)", text, re.IGNORECASE)
        if m:
            out["root_depth_mm"] = int(m.group(1))
    return out


def main():
    rows = []
    correct = 0
    total = 0
    for stage, das in STAGE_DAS.items():
        try:
            j = call_rag(das)
            got = extract_params(j)
            err = j.get("irrigation", {}).get("status") == "error"
        except Exception as e:
            got = {"_err": str(e)}
            err = True

        truth = GROUND_TRUTH[stage]
        per_field = {}
        for k, v in truth.items():
            g = got.get(k)
            ok = (g is not None) and abs(float(g) - float(v)) < 1e-6
            per_field[k] = (g, ok)
            total += 1
            if ok:
                correct += 1
        rows.append((stage, das, truth, got, per_field, err))

    print("\nEngine 2 — Irrigation Parameter Retrieval (maize) — RAG vs source-of-truth")
    print(f"current_date={CURRENT.isoformat()}\n")
    print(f"{'stage':>6} {'DAS':>4} | {'kc (truth/RAG)':>20} | {'MAD':>15} | {'root_mm':>15}")
    print("-" * 75)
    for stage, das, truth, got, pf, err in rows:
        kc_t, (kc_g, kc_ok) = truth["kc"], pf["kc"]
        mad_t, (mad_g, mad_ok) = truth["mad"], pf["mad"]
        rd_t, (rd_g, rd_ok) = truth["root_depth_mm"], pf["root_depth_mm"]
        flag = " (ERR)" if err else ""
        print(f"{stage:>6} {das:>4} | {kc_t}/{kc_g}{' ✓' if kc_ok else ' ✗'}".ljust(40)
              + f"| {mad_t}/{mad_g}{' ✓' if mad_ok else ' ✗'}".ljust(20)
              + f"| {rd_t}/{rd_g}{' ✓' if rd_ok else ' ✗'}{flag}")
    print()
    print(f"Field-level accuracy: {correct}/{total} = {100*correct/total:.1f}%")


if __name__ == "__main__":
    main()
