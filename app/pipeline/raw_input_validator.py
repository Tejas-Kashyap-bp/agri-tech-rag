"""
Block: Raw Input Validation

Runs the deterministic structure/range/logical checks from validator.py on the
user's PARSED STRUCTURED INPUT (JSON dict or list-of-row-dicts from CSV) before
the LLM extractor sees it.

Why this block exists:
  The LLM extractor silently heals invalid inputs — it fills in nulls, clamps
  out-of-range values, and guesses missing fields. If we only validated the
  LLM's output, bad uploads would slip through. Running validation on the raw
  parsed input is the real guardrail.

  For unstructured inputs (PDFs) raw_structured is None and this block is a
  no-op; the post-extraction validation pass in the orchestrator then carries
  the weight.
"""

from typing import Any

from app.pipeline import validator
from app.schemas import Classification

BLOCK = "Raw Input Validation"


def validate_raw(
    raw_structured: Any,
    classification: Classification,
) -> None:
    """
    Validate the user-provided structured data against the classification.

    - raw_structured is None           → PDF or unstructured input; skip.
    - raw_structured is a dict         → validate as a document object.
    - raw_structured is list[dict]     → wrap as {"rows": [...]} so the same
                                          range/structure walker applies.
    """
    if raw_structured is None:
        return

    if isinstance(raw_structured, list):
        doc: dict[str, Any] = {"rows": raw_structured}
    elif isinstance(raw_structured, dict):
        doc = raw_structured
    else:
        # Scalars / unexpected shapes — nothing sensible to check.
        return

    validator.validate(doc, classification, block=BLOCK)
