"""
models.py — Shared data contract for the soil pipeline.

Each parameter is stored as an individual record (value, source, confidence,
unit, etc.) so downstream engines know exactly how reliable each value is.

Nutrient keys (must match soil_master.nutrient_key exactly):
    nitrogen_N, phosphorus_P, potassium_K, soil_pH,
    organic_carbon_OC, zinc_Zn, iron_Fe, manganese_Mn,
    copper_Cu, boron_B, sulphur_S, ec_EC
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


# ── Canonical nutrient keys ───────────────────────────────────────────────────
# Must match soil_master.nutrient_key values exactly.
NUTRIENT_KEYS = [
    "nitrogen_N",
    "phosphorus_P",
    "potassium_K",
    "soil_pH",
    "organic_carbon_OC",
    "zinc_Zn",
    "iron_Fe",
    "manganese_Mn",
    "copper_Cu",
    "boron_B",
    "sulphur_S",
    "ec_EC",            # Electrical Conductivity (dS/m) — added
]

# Minimum keys required by the fertilizer engine
REQUIRED_KEYS = ["nitrogen_N", "phosphorus_P", "potassium_K", "soil_pH", "organic_carbon_OC"]

# Confidence score per data source — used to tag every parameter value
CONFIDENCE_SCORES: Dict[str, float] = {
    "farmer":      1.0,   # Ground truth from farmer's soil report
    "soilgrids":   0.6,   # Model-based estimate from ISRIC API
    "supabase":    0.4,   # Regional average from district-level table
    "unavailable": 0.0,   # No source could provide this parameter
}

# Unit registry — canonical unit per parameter.
# All layers MUST normalize to these units before calling SoilData.set().
# Conversions: kg/acre × 2.471 → kg/ha | g/kg ÷ 10 → % | mg/kg ≡ ppm
UNIT_REGISTRY: Dict[str, Dict] = {
    "nitrogen_N":        {"canonical": "kg/ha",  "aliases": ["kg/acre", "mg/kg", "%"]},
    "phosphorus_P":      {"canonical": "kg/ha",  "aliases": ["kg/acre", "mg/kg", "ppm"]},
    "potassium_K":       {"canonical": "kg/ha",  "aliases": ["kg/acre", "mg/kg", "ppm"]},
    "soil_pH":           {"canonical": None,     "aliases": []},
    "organic_carbon_OC": {"canonical": "%",      "aliases": ["g/kg"]},
    "zinc_Zn":           {"canonical": "ppm",    "aliases": ["mg/kg"]},
    "iron_Fe":           {"canonical": "ppm",    "aliases": ["mg/kg"]},
    "manganese_Mn":      {"canonical": "ppm",    "aliases": ["mg/kg"]},
    "copper_Cu":         {"canonical": "ppm",    "aliases": ["mg/kg"]},
    "boron_B":           {"canonical": "ppm",    "aliases": ["mg/kg"]},
    "sulphur_S":         {"canonical": "ppm",    "aliases": ["mg/kg"]},
    "ec_EC":             {"canonical": "dS/m",   "aliases": ["mS/cm"]},
}


@dataclass
class SoilData:
    """
    Per-parameter soil data store.

    Instead of a single dataset-level source/confidence, every nutrient key
    carries its own record so downstream engines can selectively trust or
    distrust individual values.

    Usage:
        data = SoilData(state="Haryana", district="Karnal", ...)
        data.set("nitrogen_N", 120.0, source="farmer", confidence=1.0, unit="kg/ha")
        print(data.to_dict())
    """

    # Core values — None means sentinel (all sources failed for this key)
    nutrients:    Dict[str, Optional[float]] = field(default_factory=dict)

    # Per-parameter metadata
    sources:      Dict[str, str]             = field(default_factory=dict)   # {key: "farmer"|"soilgrids"|...}
    confidences:  Dict[str, float]           = field(default_factory=dict)   # {key: 0.0–1.0}
    units:        Dict[str, str]             = field(default_factory=dict)   # {key: "kg/ha"|"ppm"|...}
    ideal_ranges: Dict[str, str]             = field(default_factory=dict)   # {key: "6.0-7.5"}
    statuses:     Dict[str, str]             = field(default_factory=dict)   # {key: "Low"|"Normal"|"High"}
    methods:      Dict[str, str]             = field(default_factory=dict)   # {key: "range_mean"|"exact"}

    # Overall pipeline confidence — set once at pipeline completion
    overall_confidence: float = 0.0

    # Location context (used for master table lookup)
    state:      Optional[str] = None
    district:   Optional[str] = None
    soil_type:  Optional[str] = None
    crop:       Optional[str] = None

    # ------------------------------------------------------------------ helpers

    def set(
        self,
        key: str,
        value: Optional[float],
        *,
        source:      str           = "unknown",
        confidence:  float         = 0.0,
        unit:        Optional[str] = None,
        ideal_range: Optional[str] = None,
        status:      Optional[str] = None,
        method:      Optional[str] = None,
    ):
        """Set a parameter value with its full metadata record."""
        self.nutrients[key]   = round(float(value), 4) if value is not None else None
        self.sources[key]     = source
        self.confidences[key] = confidence
        if unit:        self.units[key]        = unit
        if ideal_range: self.ideal_ranges[key] = ideal_range
        if status:      self.statuses[key]     = status
        if method:      self.methods[key]      = method

    def get(self, key: str) -> Optional[float]:
        return self.nutrients.get(key)

    def missing(self) -> list:
        """Required keys that have no real value (absent or sentinel None)."""
        return [k for k in REQUIRED_KEYS if self.nutrients.get(k) is None]

    def is_complete(self) -> bool:
        return len(self.missing()) == 0

    def to_dict(self) -> Dict[str, Any]:
        """
        Flat dict where each nutrient key maps to its full record,
        plus overall_confidence and meta at the top level.

        Example output:
          {
            "nitrogen_N":  {"value": 120, "source": "farmer",  "confidence": 1.0, "unit": "kg/ha"},
            "phosphorus_P": {"value": 17.5, "source": "supabase", "confidence": 0.4, "method": "range_mean"},
            "ec_EC":        {"value": None, "source": "unavailable", "confidence": 0.0},
            "overall_confidence": 0.67,
            "meta": {"state": "Haryana", ...}
          }
        """
        result: Dict[str, Any] = {}

        for key in NUTRIENT_KEYS:
            record: Dict[str, Any] = {
                "value":      self.nutrients.get(key),
                "source":     self.sources.get(key, "unavailable"),
                "confidence": self.confidences.get(key, 0.0),
            }
            # Only include optional fields if they have values
            if key in self.units:        record["unit"]        = self.units[key]
            if key in self.ideal_ranges: record["ideal_range"] = self.ideal_ranges[key]
            if key in self.statuses:     record["status"]      = self.statuses[key]
            if key in self.methods:      record["method"]      = self.methods[key]
            result[key] = record

        result["overall_confidence"] = self.overall_confidence
        result["meta"] = {
            "state":     self.state,
            "district":  self.district,
            "soil_type": self.soil_type,
            "crop":      self.crop,
        }
        return result

    def __repr__(self):
        filled  = sum(1 for v in self.nutrients.values() if v is not None)
        total   = len(NUTRIENT_KEYS)
        missing = [k for k in NUTRIENT_KEYS if self.nutrients.get(k) is None]
        return (
            f"<SoilData {filled}/{total} filled | "
            f"overall_confidence={self.overall_confidence:.2f} | "
            f"missing={missing}>"
        )
