"""
Block: Evidence Checker

Post-extraction guardrail. Walks the extracted document looking for fields
where the LLM assigned a non-null value but provided no supporting source text
(i.e. `<field>_source` is null or absent).

These are inferred values — the LLM used domain knowledge rather than text
from the document. They are flagged so a human can review and confirm.

Two public functions:
  find_unsupported_fields(doc) → list of dotted field paths that were inferred
  strip_source_fields(doc)     → clean copy of doc with all *_source keys removed
"""

import re
from typing import Any

BLOCK = "Evidence Checker"

# Fields that don't carry document-level source evidence:
#   - crop, doc_type, engine: derived from filename/classification, only
#     ever appear at the top of the document.
#   - notes, general_rules: free-text operator commentary. The LLM
#     legitimately rephrases these instead of quoting verbatim, so demanding
#     a substring source produces false positives that route otherwise-clean
#     uploads to human review. Trade-off: we lose the ability to verify
#     these specific fields were grounded in the document. We accept that
#     because false-positive review queues erode reviewer attention — and an
#     inattentive reviewer is more dangerous to data quality than an
#     unverified free-text note. Reviewer trust is itself a finite resource.
_TOP_LEVEL_SKIP = {"crop", "doc_type", "engine"}
_ANY_DEPTH_SKIP = {"notes", "general_rules"}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_NUMBER_TOL = 1e-6
_PROXIMITY_WINDOW = 50

# Keyword proximity map: a numeric match in source_text only counts if the
# CLOSEST known keyword inside ±_PROXIMITY_WINDOW characters maps to this
# field. "Closest-wins" is needed (not any-in-window) because DAS bounds and
# Kc values co-occur in the same table sentence, e.g.
# "DAS 55 to 90, the Kc is 1.15" — here "Kc" is within 50 chars of "55" but
# "DAS" is closer, so 55 must NOT count as a Kc value. Distractor keywords
# (das/stage/day) are registered to preempt accidental matches.
FIELD_KEYWORDS: dict[str, list[str]] = {
    "kc": ["kc"],
    "mad": ["mad"],
    "root_depth_mm": ["root", "depth"],
    "ndvi_range": ["ndvi"],
}

_DISTRACTOR = "__distractor__"
_ALL_KEYWORDS: dict[str, str] = {
    "das": _DISTRACTOR,
    "day": _DISTRACTOR,
    "stage": _DISTRACTOR,
}
for _f, _kws in FIELD_KEYWORDS.items():
    for _kw in _kws:
        _ALL_KEYWORDS[_kw] = _f

# Catches "DAS 22 to 55", "DAS 55-90", "stage 1 to 3", etc. Any number whose
# span sits inside the matched range is a distractor number — it belongs to
# DAS/stage, not to Kc/MAD/root. Without this pre-pass, "the Kc" in the same
# sentence as "DAS 22 to 55" is sometimes closer to "55" than "DAS" is, and
# the closest-keyword-wins rule alone accepts 55 as Kc.
_RANGE_PATTERN = re.compile(
    r"(das|stage|day)\s+-?\d+(?:\.\d+)?\s*(?:to|[-–—])\s*-?\d+(?:\.\d+)?",
    re.IGNORECASE,
)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def find_unsupported_fields(doc: dict[str, Any], raw_text: str = "") -> list[str]:
    """
    Return dotted field paths that fail any of:
      1. Value is non-null but `<field>_source` is null/absent.
      2. `<field>_source` string is not a substring of raw_text (hallucinated).
      3. Value is numeric and no number in `<field>_source` matches it.

    Example: ["stages[0].kc", "stages[2].mad"]
    """
    results: list[str] = []
    _scan(doc, "", results, raw_text, top_level=True)
    return results


def strip_source_fields(doc: Any) -> Any:
    """Remove all *_source companion fields before downstream pipeline stages."""
    if isinstance(doc, dict):
        return {
            k: strip_source_fields(v)
            for k, v in doc.items()
            if not k.endswith("_source")
        }
    if isinstance(doc, list):
        return [strip_source_fields(item) for item in doc]
    return doc


def _scan(
    obj: Any,
    path: str,
    results: list[str],
    raw_text: str,
    top_level: bool = False,
) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_source"):
                continue
            if k in _ANY_DEPTH_SKIP:
                continue
            if top_level and k in _TOP_LEVEL_SKIP:
                continue

            full_path = f"{path}.{k}" if path else k
            source_key = f"{k}_source"

            if v is not None and not isinstance(v, (dict, list)):
                source_val = obj.get(source_key)
                if source_val is None:
                    results.append(full_path)
                elif not _source_supports_value(v, source_val, raw_text, k):
                    results.append(full_path)

            _scan(v, full_path, results, raw_text, top_level=False)

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan(item, f"{path}[{i}]", results, raw_text, top_level=False)


def _source_supports_value(
    value: Any, source_val: Any, raw_text: str, field_name: str = ""
) -> bool:
    """Return False if the source is hallucinated, numbers disagree, or the
    matching number is not near the field keyword in the source."""
    if not isinstance(source_val, str) or not source_val.strip():
        return False

    # Source existence — case-folded + whitespace-normalized on both sides so
    # PDF column spacing and title-case quoting do not cause spurious flags.
    if raw_text:
        if _normalize(source_val).lower() not in _normalize(raw_text).lower():
            return False

    # Numeric value-source consistency with keyword-proximity guard.
    # (bool is a subclass of int in Python — exclude it.)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        matches = list(_NUMBER_RE.finditer(source_val))
        if not matches:
            return False

        field_keywords = FIELD_KEYWORDS.get(field_name)
        source_lower = source_val.lower()
        distractor_spans = [
            (r.start(), r.end()) for r in _RANGE_PATTERN.finditer(source_lower)
        ]
        for m in matches:
            try:
                if abs(float(m.group()) - float(value)) >= _NUMBER_TOL:
                    continue
            except ValueError:
                continue
            # Number matches. No keyword policy for this field → accept.
            if field_keywords is None:
                return True
            # Numbers inside a "DAS N to M" style span are always distractors.
            if any(s <= m.start() and m.end() <= e for s, e in distractor_spans):
                continue
            if _nearest_keyword_supports_field(
                source_lower, m.start(), m.end(), field_name
            ):
                return True
        return False

    return True


def _nearest_keyword_supports_field(
    source_lower: str, num_start: int, num_end: int, field_name: str
) -> bool:
    """Within ±_PROXIMITY_WINDOW chars of the numeric match, find the closest
    known keyword (including distractors). Accept only if it maps to field_name."""
    win_start = max(0, num_start - _PROXIMITY_WINDOW)
    win_end = min(len(source_lower), num_end + _PROXIMITY_WINDOW)
    window = source_lower[win_start:win_end]

    best_distance = None
    best_field: str | None = None
    for kw, kw_field in _ALL_KEYWORDS.items():
        idx = window.find(kw)
        while idx != -1:
            kw_abs_start = win_start + idx
            kw_abs_end = kw_abs_start + len(kw)
            # Distance from keyword to the numeric span (0 if they touch/overlap).
            if kw_abs_end <= num_start:
                dist = num_start - kw_abs_end
            elif kw_abs_start >= num_end:
                dist = kw_abs_start - num_end
            else:
                dist = 0
            if best_distance is None or dist < best_distance:
                best_distance = dist
                best_field = kw_field
            idx = window.find(kw, idx + 1)

    if best_field is None:
        return False
    return best_field == field_name
