"""
Unit tests for the evidence checker's source-supports-value logic.

WHY this exists:
  evidence_checker._source_supports_value enforces three things at once —
  source string is a substring of raw_text, the source contains a number that
  matches the field value, and the closest registered keyword to that number
  maps to the field. The "closest keyword wins" rule is the load-bearing
  defense against the DAS-vs-Kc adversarial case explicitly called out in the
  module's comments. That rule has zero pre-existing tests; if a refactor
  drops the closest-wins semantics in favor of any-keyword-in-window, bad
  values flow into the vector store silently.

Cases here are picked to lock down: (a) the happy path, (b) hallucinated
sources, (c) numeric mismatch, (d) the DAS/Kc proximity adversarial case,
(e) the range-pattern distractor pre-pass.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.evidence_checker import (  # noqa: E402
    _source_supports_value,
    find_unsupported_fields,
)


def test_source_supports_kc_when_keyword_is_closest():
    raw = "For the reproductive stage, the Kc is 1.15 per the FAO table."
    src = "the Kc is 1.15"
    assert _source_supports_value(1.15, src, raw, field_name="kc") is True


def test_hallucinated_source_rejected():
    raw = "For the reproductive stage, the Kc is 1.15."
    src = "the Kc is 1.15 according to ICAR"
    assert _source_supports_value(1.15, src, raw, field_name="kc") is False


def test_numeric_mismatch_rejected():
    raw = "For the reproductive stage, the Kc is 1.15."
    src = "the Kc is 1.15"
    assert _source_supports_value(1.20, src, raw, field_name="kc") is False


def test_das_range_distractor_does_not_count_as_kc():
    raw = "DAS 55 to 90, the Kc is 1.15"
    src = "DAS 55 to 90, the Kc is 1.15"
    assert _source_supports_value(55, src, raw, field_name="kc") is False
    assert _source_supports_value(1.15, src, raw, field_name="kc") is True


def test_skip_field_at_nested_depth_not_flagged():
    raw = "any text"
    doc = {
        "stages": [
            {"stage_code": "vegetative", "stage_code_source": "vegetative",
             "notes": "operator rephrased free text"},
        ],
    }
    flagged = find_unsupported_fields(doc, raw)
    assert "stages[0].notes" not in flagged


def test_inferred_value_without_source_is_flagged():
    raw = "Kc is 1.15"
    doc = {"stages": [{"kc": 1.15}]}
    flagged = find_unsupported_fields(doc, raw)
    assert "stages[0].kc" in flagged
