"""
Compare Engine 1 (crop stage detection) between:
  - agri-integrated (rule-based, ground truth)  http://localhost:8000/eng1
  - agri-rag       (RAG-based, under test)      http://localhost:8765/advisory/eng1

Reports per-case stage code agreement and overall accuracy.
"""
import json
from datetime import date, timedelta

import requests

INTEGRATED = "http://localhost:8000/eng1"
RAG = "http://localhost:8765/advisory/eng1"

# Maize stage boundaries (from data/crop_stage_config.json):
#   S1 0-14, S2 15-40, S3 41-65, S4 66-95, S5 96-200
# Pick DAS that hit each stage incl. boundaries.
DAS_CASES = [5, 14, 20, 40, 50, 65, 80, 100]

CURRENT = date(2026, 4, 29)


def call_integrated(crop: str, das: int) -> dict:
    sowing = (CURRENT - timedelta(days=das)).isoformat()
    body = {
        "farm_id": "F-cmp",
        "crop_type": crop,
        "sowing_date": sowing,
        "current_date": CURRENT.isoformat(),
        "ndvi_timeseries": [],   # rule-based engine works from DAS alone
        "language": "English",
    }
    r = requests.post(INTEGRATED, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def call_rag(crop: str, das: int) -> dict:
    sowing = (CURRENT - timedelta(days=das)).isoformat()
    body = {
        "crop": crop,
        "sowing_date": sowing,
        "current_date": CURRENT.isoformat(),
    }
    r = requests.post(RAG, json=body, timeout=120)
    r.raise_for_status()
    return r.json()


def extract_int_stage(j: dict) -> str | None:
    raw = j.get("raw_engine_output", {})
    return raw.get("stage_code") or raw.get("code")


import re

def extract_rag_stage(j: dict) -> str | None:
    stage = j.get("stage", {}) or {}
    details = stage.get("details", {}) or {}
    cur = details.get("current_stage", {}) or {}
    code = cur.get("stage_code") or cur.get("code") or details.get("stage_code")
    if code:
        return code
    # Fallback: scrape from summary / reasoning text (Gemini sometimes
    # changes JSON shape across calls — find an "S1".."S5" mention).
    text = " ".join([
        stage.get("summary", "") or "",
        details.get("reasoning", "") or "",
        cur.get("stage_name", "") or "",
    ])
    m = re.search(r"\bS[1-5]\b", text)
    return m.group(0) if m else None


def main():
    crop = "maize"
    rows = []
    correct = 0
    for das in DAS_CASES:
        try:
            gi = call_integrated(crop, das)
            int_stage = extract_int_stage(gi)
        except Exception as e:
            int_stage = f"ERR:{e}"
        try:
            gr = call_rag(crop, das)
            rag_stage = extract_rag_stage(gr)
            if rag_stage is None:
                # Dump the response so we can see what Gemini returned.
                with open(f"/tmp/rag_dump_das{das}.json", "w") as f:
                    json.dump(gr, f, indent=2)
        except Exception as e:
            rag_stage = f"ERR:{e}"

        match = int_stage == rag_stage
        if match:
            correct += 1
        rows.append((das, int_stage, rag_stage, match))

    print(f"\nEngine 1 — Crop Stage Detection — crop={crop}")
    print(f"current_date={CURRENT.isoformat()}\n")
    print(f"{'DAS':>5} | {'truth (integrated)':>20} | {'rag (gemini)':>14} | match")
    print("-" * 60)
    for das, i, r, m in rows:
        print(f"{das:>5} | {str(i):>20} | {str(r):>14} | {'✓' if m else '✗'}")
    print()
    print(f"Accuracy: {correct}/{len(DAS_CASES)} = {100*correct/len(DAS_CASES):.1f}%")


if __name__ == "__main__":
    main()
