"""
Block: Heuristic Pre-Filter

Before asking the LLM to classify, run cheap regex checks to produce a short
list of `possible_types`. This does two things:
  1. Gives the LLM hints, which improves classification accuracy.
  2. Makes errors easier to debug — if possible_types is empty, the document
     might not even match any known pattern.

Returns the list (possibly empty). Never raises — the LLM can still handle
documents that don't match any heuristic.
"""

import re

# Patterns are intentionally loose — we want recall here, not precision.
# The LLM does the actual decision. These are just hints.
_PATTERNS: dict[str, list[re.Pattern]] = {
    "stage_definition": [
        re.compile(r"\bstage\b", re.I),
        re.compile(r"\b(DAS|days after sowing)\b", re.I),
        re.compile(r"\bS[1-9]\b"),
    ],
    "fertigation_schedule": [
        re.compile(r"\bfertigation\b", re.I),
        re.compile(r"\b(NPK|nitrogen|phosphor|potass)\b", re.I),
        re.compile(r"\bdose\b", re.I),
    ],
    "ipm_schedule": [
        re.compile(r"\bIPM\b"),
        re.compile(r"\b(spray|pesticide|insecticide|pheromone|trap)\b", re.I),
    ],
    "pest_disease_condition_rule": [
        re.compile(r"\b(pest|disease)\b", re.I),
        re.compile(r"\bpest_disease\b", re.I),
        re.compile(r"\brule_type\b", re.I),
        re.compile(r"\b(min_das|max_das|min_temp_c|max_temp_c|min_humidity|max_humidity)\b", re.I),
        re.compile(r"\b(symptoms|severity|growth_stage)\b", re.I),
    ],
    "crop_parameters": [
        re.compile(r"\bNDVI\b"),
        re.compile(r"\bKc\b"),
        re.compile(r"\b(crop coefficient|canopy)\b", re.I),
    ],
    "condition_rule": [
        re.compile(r"\bif\b.*\bthen\b", re.I | re.DOTALL),
        re.compile(r"\b(threshold|trigger)\b", re.I),
    ],
    "treatment_mapping": [
        re.compile(r"\b(treatment|remedy|cure)\b", re.I),
    ],
    "guardrail": [
        re.compile(r"\b(do not|must not|forbidden|avoid)\b", re.I),
        re.compile(r"\b(safety|limit)\b", re.I),
    ],
}


def prefilter(text: str) -> list[str]:
    """Return the list of doc_types whose patterns matched at least once."""
    matched: list[str] = []
    for doc_type, patterns in _PATTERNS.items():
        if any(p.search(text) for p in patterns):
            matched.append(doc_type)
    return matched


# ---------------------------------------------------------------------------
# Engine family map — used to detect documents that span multiple domains
# ---------------------------------------------------------------------------

# Shared / supporting types map to None — they don't count toward ambiguity.
_DOC_TYPE_TO_ENGINE: dict[str, str | None] = {
    "stage_definition":      "e1_stage",
    # irrigation_parameters / crop_parameters: e2 removed for apple. Mapped to
    # None so a stray legacy doc still ingests cleanly without forcing engine
    # ambiguity in the heuristic check.
    "irrigation_parameters": None,
    "crop_parameters":       None,
    "fertigation_schedule":  "e3_nutrition",
    "ipm_schedule":               "e4_pest_disease_risk",
    "pest_disease_condition_rule":"e4_pest_disease_risk",
    "yield_parameters":      "e5_yield",
    # market_data: e6 removed for apple — leave None so a stray legacy doc
    # still ingests without forcing engine ambiguity.
    "market_data":           None,
    # supporting types — don't force ambiguity
    "condition_rule":        None,
    "treatment_mapping":     None,
    "guardrail":             None,
    "agronomic_knowledge":   None,
    "crop_knowledge":        None,
}


def spans_multiple_engines(possible_types: list[str]) -> bool:
    """
    True when the heuristic matched patterns belonging to two or more distinct
    engine families. That's a strong hint that the document mixes content
    domains (e.g. irrigation + fertigation + stage info in one file) and
    therefore deserves human confirmation regardless of LLM confidence.
    """
    engines = {_DOC_TYPE_TO_ENGINE.get(t) for t in possible_types}
    engines.discard(None)
    return len(engines) >= 2
