"""
Satellite Data Layer — Demo Mode.

Returns synthesized NDVI / NDRE readings for a given farm_id. Values vary
slightly per call (small jitter on top of a farm-stable baseline) so the UI
behaves like a real satellite feed that updates between requests, without
ever reaching out to an external API.

This is intentionally isolated from the cross-repo agri-integrated shim in
data_fetchers/__init__.py — that module re-exports production fetchers
(Sentinel Hub etc.) and pulling demo numbers through it would muddy the
distinction. Callers import this module by its explicit path.
"""

import random
from typing import Any, Optional


def _baseline_for(farm_id: Optional[str]) -> tuple[float, float, float]:
    """Per-farm stable baseline so the same farm doesn't swing wildly between
    calls. Derived from a hash of farm_id so it is deterministic."""
    seed = abs(hash(str(farm_id or "demo-default"))) % (2**32)
    rng = random.Random(seed)
    return (
        rng.uniform(0.45, 0.78),  # ndvi baseline
        rng.uniform(-0.05, 0.05),  # ndvi 7d delta baseline
        rng.uniform(0.25, 0.50),  # ndre baseline
    )


def get_satellite_data(farm_id: Optional[str] = None) -> dict[str, Any]:
    """Return a dict with ndvi_current, ndvi_delta_7d, ndre_current.

    Output keys are stable; numeric values are jittered around a per-farm
    baseline to simulate a refreshed satellite reading."""
    ndvi_base, delta_base, ndre_base = _baseline_for(farm_id)
    jitter = random.Random()
    ndvi_current = max(0.0, min(1.0, ndvi_base + jitter.uniform(-0.04, 0.04)))
    ndvi_delta_7d = max(-0.30, min(0.30, delta_base + jitter.uniform(-0.03, 0.03)))
    ndre_current = max(0.0, min(1.0, ndre_base + jitter.uniform(-0.03, 0.03)))
    # EVI tracks NDVI loosely (canopy density follows greenness) but is
    # treated as supportive-only by Engine 5 — kept here so all callers see
    # one consistent satellite snapshot per request.
    evi_current = max(0.0, min(1.0, (ndvi_base * 0.7) + jitter.uniform(-0.04, 0.04)))
    return {
        "ndvi_current": round(ndvi_current, 3),
        "ndvi_delta_7d": round(ndvi_delta_7d, 3),
        "ndre_current": round(ndre_current, 3),
        "evi_current": round(evi_current, 3),
        "source": "demo",
    }
