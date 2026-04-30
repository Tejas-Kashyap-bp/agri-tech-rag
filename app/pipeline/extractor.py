"""
Block: Structured Extraction

Takes raw text + a confirmed classification, asks the LLM to produce a
structured JSON representation of the document.

STRICT EXTRACTION RULES (Phase 1 design decision):
  - LLM must NOT infer or fabricate values
  - For every non-null field, a companion `<field>_source` must quote the
    exact text that supports it
  - If LLM cannot find source text, it must set the value to null
  - Evidence checker (evidence_checker.py) reads the `*_source` fields to
    flag inferred values for human review

Retry policy:
  - first attempt fails to produce valid JSON → retry once with tighter prompt
  - second attempt also fails → raise PipelineError naming this block
"""

import json
from typing import Any

from app.llm.gemini_provider import llm
from app.schemas import Classification, PipelineError

BLOCK = "Structured Extraction"

_SYSTEM = """You are a precise agricultural data extractor.
Extract only what is explicitly written in the document. Do not add, infer, or assume values.

STRICT RULES — follow exactly:
1. If a field value is NOT explicitly stated in the document, set it to null.
2. Do NOT fill in reasonable defaults or use domain knowledge to complete missing values. If the document doesn't say it, it's null.
3. For every field you extract with a non-null value, add a companion field named {fieldname}_source containing the EXACT phrase or sentence from the document that supports that value.
4. If you assign a non-null value but cannot quote specific source text for it, you MUST set {fieldname}_source to null. This is how you signal that the value was inferred.
5. For list items (e.g. inside a "stages" array), apply rules 3 and 4 to each item's fields individually.

null values are correct and expected when data is absent — they are better than inferred values.
Inferred values with null source will be flagged for human review.

Return JSON only. No prose, no markdown fences.
"""

_SYSTEM_RETRY = """You are a precise agricultural data extractor.
Your previous response was not valid JSON. Return a JSON object only — no prose, no markdown.

IMPORTANT: For every non-null field `foo`, include `foo_source` with the exact supporting text, or null if inferred.
Missing values should be null. Do not infer or fabricate.
"""


def extract(text: str, classification: Classification) -> dict[str, Any]:
    prompt = (
        f"Classification: engine={classification.engine.value}, "
        f"crop={classification.crop}, doc_type={classification.doc_type.value}\n\n"
        f"Document:\n{text}\n\n"
        f"Extract this document into a structured JSON object following the strict rules."
    )

    raw = _call(prompt, _SYSTEM)
    parsed = _try_parse(raw)
    if parsed is not None:
        return parsed

    raw_retry = _call(prompt, _SYSTEM_RETRY)
    parsed = _try_parse(raw_retry)
    if parsed is not None:
        return parsed

    raise PipelineError(
        block=BLOCK,
        reason="LLM failed to produce valid structured JSON after 2 attempts",
        detail=f"First: {raw[:200]} | Retry: {raw_retry[:200]}",
        action_required="Re-upload a cleaner or pre-structured document",
    )


def _call(prompt: str, system: str) -> str:
    try:
        return llm.complete_json(prompt=prompt, system=system)
    except Exception as exc:
        raise PipelineError(
            block=BLOCK,
            reason="LLM API call failed during extraction",
            detail=str(exc),
            action_required="Retry the upload or check LLM service status",
        ) from exc


def _try_parse(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data
