"""
Engine 5 yield-calculation layer.

Pure functions, no I/O. Computes a deterministic base yield from tree
geometry (TCSA × crop density × fruit weight) and applies a satellite-driven
correction percentage as a *post-hoc* adjustment. Base yield is the source
of truth — satellite signals only nudge the final number; they never feed
into the base formula.

Kept isolated from satellite_layer.py because Engine 3 and Engine 5 use
different NDVI / NDRE / EVI bucket thresholds and different advisory copy.
Sharing one classifier would entangle the two engines.
"""

import math
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Step 1 — base yield (do not modify)
# ---------------------------------------------------------------------------

def compute_base_yield(
    radius_of_tree: float,
    crop_density: float,
    average_fruit_weight_g: float,
) -> float:
    """Base yield in kg/acre.

    TCSA       = π * r²
    fruit_count = TCSA * crop_density
    base_yield  = (fruit_count * fruit_weight_g) / 1000
    """
    tcsa = math.pi * (radius_of_tree ** 2)
    fruit_count = tcsa * crop_density
    return (fruit_count * average_fruit_weight_g) / 1000.0


# ---------------------------------------------------------------------------
# Step 3 — satellite interpretation (Engine-5 specific bands)
# ---------------------------------------------------------------------------

def _ndvi_status(ndvi: float) -> str:
    if ndvi < 0.4:
        return "LOW"
    if ndvi <= 0.6:
        return "MEDIUM"
    return "HIGH"


def _ndre_status(ndre: float) -> str:
    if ndre < 0.25:
        return "LOW"
    if ndre <= 0.35:
        return "MEDIUM"
    return "HIGH"


def _evi_status(evi: float) -> str:
    if evi < 0.3:
        return "LOW"
    if evi <= 0.5:
        return "MEDIUM"
    return "HIGH"


def classify_yield_satellite(sat: dict[str, Any]) -> Optional[dict[str, str]]:
    """Return NDVI / NDRE / EVI buckets, or None if any value is missing /
    non-numeric (caller falls back to zero adjustment)."""
    try:
        ndvi = float(sat["ndvi_current"])
        ndre = float(sat["ndre_current"])
        evi = float(sat["evi_current"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "ndvi_status": _ndvi_status(ndvi),
        "ndre_status": _ndre_status(ndre),
        "evi_status": _evi_status(evi),
    }


# ---------------------------------------------------------------------------
# Step 4 — adjustment percent (EVI is supportive only, never primary)
# ---------------------------------------------------------------------------

def compute_adjustment_percent(features: dict[str, str]) -> int:
    ndvi = features.get("ndvi_status")
    ndre = features.get("ndre_status")
    if ndvi == "LOW" and ndre == "LOW":
        return -15
    if ndvi == "LOW":
        return -10
    if ndvi == "HIGH" and ndre == "HIGH":
        return 10
    return 0


# ---------------------------------------------------------------------------
# Step 5 — final yield
# ---------------------------------------------------------------------------

def compute_final_yield(base_yield: float, adjustment_percent: float) -> float:
    return round(base_yield * (1 + adjustment_percent / 100.0), 2)


# ---------------------------------------------------------------------------
# Step 6 — exact summary string (DO NOT change wording / order)
# ---------------------------------------------------------------------------

def build_yield_summary(
    base_yield: float, adjustment_percent: float, final_yield: float
) -> str:
    # Per-tree summary line. Total-orchard wording is built in the
    # orchestrator once tree_count is known.
    return (
        f"Per-tree yield: about {_fmt(final_yield)} kg per tree."
    )


def build_total_yield_summary(
    final_per_tree: float, num_trees: int, total_yield: float
) -> str:
    """Farmer-facing summary covering both per-tree and total orchard yield.
    No mention of NDVI / NDRE / EVI or any technical index."""
    return (
        f"For each tree, expected yield is about {_fmt(final_per_tree)} kg. "
        f"Across your {num_trees} trees, the total expected yield is about "
        f"{_fmt(total_yield)} kg."
    )


def _fmt(n: float) -> str:
    # Integer when it is one (e.g. 3000), else 2-decimal (e.g. 2550.25).
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.2f}"


# ---------------------------------------------------------------------------
# Step 7 — satellite advisory copy
# ---------------------------------------------------------------------------

def build_satellite_advisory(
    features: dict[str, str], adjustment_percent: int = 0
) -> str:
    """Plain-language note for the farmer explaining how recent field
    condition is shaping the yield estimate. No NDVI / NDRE / EVI mention.

    The adjustment percentage is woven into the sentence as a subtle hint
    that the number was nudged by an observed signal — phrased as a
    'recent field-health check' so the farmer sees there is data behind
    the change without us naming the underlying satellite indices."""
    ndvi = features["ndvi_status"]
    ndre = features["ndre_status"]
    pct = abs(int(adjustment_percent))
    if ndvi == "LOW" and ndre == "LOW":
        return (
            f"A recent field-health check of the orchard suggests the trees "
            f"are looking weak and the leaves are pale. Because of this, the "
            f"yield estimate has been reduced by about {pct}% compared with "
            f"the standard calculation."
        )
    if ndvi == "HIGH" and ndre == "HIGH":
        return (
            f"A recent field-health check of the orchard suggests the trees "
            f"are strong and healthy. Because of this, the yield estimate "
            f"has been lifted by about {pct}% over the standard calculation."
        )
    return (
        "A recent field-health check of the orchard shows the trees are in "
        "fair shape overall — most are growing well, with a few patches "
        "looking a little weaker. The yield estimate is in line with what a "
        "healthy orchard of this size and age would be expected to produce."
    )
