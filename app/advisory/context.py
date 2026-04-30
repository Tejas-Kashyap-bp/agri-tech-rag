"""
Advisory request context.

This is the input envelope for the multi-engine advisory flow. It carries the
farm/field state that the LLM needs to reason over the retrieved knowledge.

WHY this is a single flexible context (not per-engine inputs):
  All 5 engines run together (see spec section 5: "Multi-Engine Execution
  Flow"). They share the same farm + crop + date + sensor snapshot. Splitting
  inputs per engine would invite drift (e.g. one engine seeing a different
  current_date than another). One context, computed once, passed to all.

WHY DAS is computed here (deterministic):
  Spec section 4 explicitly allows DAS as a "minimal deterministic helper".
  We do this in code rather than asking the LLM because:
    - date arithmetic has no agronomic ambiguity
    - it is used by every engine — recomputing it inside each LLM prompt
      wastes tokens and risks the LLM drifting on the value
"""

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field


class AdvisoryContext(BaseModel):
    """
    Inputs for one full advisory run.

    Only `crop`, `sowing_date`, and `current_date` are required because every
    engine needs (crop, DAS) at minimum. All sensor / detection / weather
    fields are optional — engines that need them will say so in their reasoning
    when the field is missing rather than fabricating a value.
    """

    crop: str
    sowing_date: date
    current_date: date

    # Optional sensor / external data — passed verbatim to the LLM as context.
    # Schemas are intentionally loose (dict[str, Any]) so client teams can
    # add fields without a code change. The LLM reads only what it sees.
    weather: Optional[dict[str, Any]] = None
    soil: Optional[dict[str, Any]] = None
    satellite: Optional[dict[str, Any]] = None  # ndvi/ndwi timeseries, etc.
    detection: Optional[dict[str, Any]] = None  # pest/disease detection (E4 reactive)

    # Free-form extra context. Anything the client wants the LLM to consider.
    extra: Optional[dict[str, Any]] = Field(default=None)

    @property
    def das(self) -> int:
        """Days After Sowing — single source of truth for the whole run."""
        return (self.current_date - self.sowing_date).days

    def to_prompt_block(self) -> str:
        """
        Render the context as a stable, LLM-readable text block.

        Deterministic ordering (alphabetic on optional sections) so the same
        input produces the same prompt — important for caching and for
        reproducibility during audit.
        """
        lines = [
            f"crop: {self.crop}",
            f"sowing_date: {self.sowing_date.isoformat()}",
            f"current_date: {self.current_date.isoformat()}",
            f"days_after_sowing: {self.das}",
        ]
        optional = {
            "weather": self.weather,
            "soil": self.soil,
            "satellite": self.satellite,
            "detection": self.detection,
            "extra": self.extra,
        }
        for key in sorted(optional.keys()):
            value = optional[key]
            if value is None:
                continue
            lines.append(f"{key}: {value}")
        return "\n".join(lines)
