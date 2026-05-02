"""
Lightweight feature + advisory layer over a satellite reading.

Kept deliberately tiny — pure functions, no I/O — so Engine 3 can call this
synchronously after its LLM result lands and decorate the response with
satellite-driven hints. If the inputs are missing or malformed, callers get
None back and skip the decoration; the existing fertilizer logic is the
ground truth.
"""

from typing import Any, Optional


def classify(sat: dict[str, Any]) -> Optional[dict[str, str]]:
    """Bucket NDVI / NDRE into LOW / MEDIUM / HIGH and the 7-day NDVI delta
    into RISING / STABLE / DECLINING. Returns None if any required key is
    missing or non-numeric (caller should silently skip)."""
    try:
        ndvi = float(sat["ndvi_current"])
        delta = float(sat["ndvi_delta_7d"])
        ndre = float(sat["ndre_current"])
    except (KeyError, TypeError, ValueError):
        return None

    if ndvi >= 0.65:
        ndvi_health = "HIGH"
    elif ndvi >= 0.45:
        ndvi_health = "MEDIUM"
    else:
        ndvi_health = "LOW"

    if delta > 0.03:
        ndvi_trend = "RISING"
    elif delta < -0.03:
        ndvi_trend = "DECLINING"
    else:
        ndvi_trend = "STABLE"

    if ndre >= 0.40:
        ndre_status = "HIGH"
    elif ndre >= 0.28:
        ndre_status = "MEDIUM"
    else:
        ndre_status = "LOW"

    return {
        "ndvi_health": ndvi_health,
        "ndvi_trend": ndvi_trend,
        "ndre_status": ndre_status,
    }


def build_satellite_advisory(features: dict[str, str]) -> str:
    """Farmer-friendly add-on line for the fertilizer advisory. No mention of
    NDVI / NDRE / EVI or any technical index — the field team only wants
    plain language about how the orchard looks and what to do."""
    ndvi_health = features["ndvi_health"]
    ndvi_trend = features["ndvi_trend"]
    ndre_status = features["ndre_status"]

    if ndre_status == "LOW" and ndvi_trend == "DECLINING":
        return (
            "Recent field check suggests the trees are looking weak and the "
            "leaves are losing their green colour. Apply a nitrogen fertilizer "
            "in the next few days."
        )
    if ndvi_health == "HIGH" and ndre_status in ("MEDIUM", "HIGH"):
        return (
            "The orchard is looking healthy and the leaves have enough "
            "nourishment. No extra fertilizer is needed right now."
        )
    if ndvi_health == "LOW" and ndre_status in ("MEDIUM", "HIGH"):
        return (
            "The leaves look fine but the trees overall appear weak. Please "
            "check the irrigation and look for pests or disease before adding "
            "more fertilizer."
        )
    if ndre_status == "LOW":
        return (
            "The leaves are losing some of their green colour. A small dose of "
            "nitrogen fertilizer over the next few days will help."
        )
    if ndvi_trend == "DECLINING":
        return (
            "The orchard health is going down a little. Please check the "
            "irrigation and look for pests before changing the fertilizer plan."
        )
    return (
        "The orchard is looking healthy. Please continue with the regular "
        "fertilizer plan."
    )


def build_satellite_summary(features: dict[str, str]) -> str:
    # Kept for backward compatibility but no longer surfaced in the farmer UI.
    # Returns a short, non-technical sentence that paraphrases the bucket
    # without naming the indices.
    health_word = {"HIGH": "good", "MEDIUM": "okay", "LOW": "weak"}[features["ndvi_health"]]
    trend_word = {"RISING": "improving", "STABLE": "steady", "DECLINING": "slowly going down"}[features["ndvi_trend"]]
    return f"Recent field condition: {health_word} and {trend_word}."
