"""
Block: Validation (shared checks)

Three deterministic checks on structured data:
  1. Structure — required fields present, no nulls at required paths
  2. Range     — numeric values fall inside sensible bounds
  3. Logical   — doc_type specific rules (continuity, non-overlap, etc.)

This module is called from TWO places in the pipeline:
  - raw_input_validator → runs the checks on the user-provided parsed data
    BEFORE the LLM sees it (primary guardrail — LLM cannot heal garbage it
    never touched).
  - orchestrator.post_classify_segment → runs the same checks on the LLM's
    extracted output as a safety net (catches anything the LLM introduced).

All three checks collect violations and are run to completion; a single
PipelineError reports every problem at once so the user doesn't have to
fix-then-re-upload one issue at a time.
"""

import re
from typing import Any, Iterable

from app.schemas import Classification, DocType, PipelineError

DEFAULT_BLOCK = "Validation"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate(
    doc: dict[str, Any],
    classification: Classification,
    *,
    block: str = DEFAULT_BLOCK,
) -> None:
    """Run all three checks. Raise a single PipelineError listing every issue."""
    violations: list[str] = []
    violations.extend(_check_structure(doc, classification))
    violations.extend(_check_ranges(doc))
    violations.extend(_check_logical(doc, classification))

    if violations:
        raise PipelineError(
            block=block,
            reason=f"{len(violations)} validation issue(s) found",
            detail=" | ".join(violations),
            action_required="Fix the flagged fields and re-upload the document",
        )


# ---------------------------------------------------------------------------
# Structure check — per doc_type required paths, applied recursively
# ---------------------------------------------------------------------------

# For each doc_type, declare what MUST be present and non-null.
#   required_top:       keys at the document root
#   required_in_list:   for any of these list-keys, each item must have these fields
_DOC_TYPE_STRUCTURE: dict[DocType, dict[str, Any]] = {
    DocType.STAGE_DEFINITION: {
        "required_top": ["stages"],
        "required_in_list": {
            "stages": ["stage_code", "stage_name", "start_das", "end_das"],
        },
    },
    DocType.IRRIGATION_PARAMETERS: {
        "required_top": ["stages"],
        "required_in_list": {
            "stages": ["stage_code", "stage_name", "kc", "mad", "root_depth_mm"],
        },
    },
    DocType.FERTIGATION_SCHEDULE: {
        "required_top": [],  # content varies (CSV rows vs. JSON list); leave lenient
        "required_in_list": {},
    },
    DocType.IPM_SCHEDULE: {
        "required_top": [],
        "required_in_list": {},
    },
    DocType.PEST_DISEASE_CONDITION_RULE: {
        "required_top": [],
        "required_in_list": {},
    },
    DocType.YIELD_PARAMETERS: {
        "required_top": [],
        "required_in_list": {},
    },
    DocType.MARKET_DATA: {
        "required_top": [],
        "required_in_list": {},
    },
}


def _check_structure(doc: dict[str, Any], classification: Classification) -> list[str]:
    if not isinstance(doc, dict) or not doc:
        return ["document is empty or not a JSON object"]

    spec = _DOC_TYPE_STRUCTURE.get(classification.doc_type)
    if spec is None:
        # No per-type rules for this doc_type — nothing to check.
        return []

    issues: list[str] = []

    # Top-level required fields
    for key in spec["required_top"]:
        if key not in doc:
            issues.append(f"missing required top-level field '{key}'")
        elif doc[key] is None or doc[key] == "" or doc[key] == []:
            issues.append(f"top-level field '{key}' is empty/null")

    # List-item required fields (recursive into each item)
    for list_key, required_fields in spec["required_in_list"].items():
        items = doc.get(list_key)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                issues.append(f"{list_key}[{i}] is not an object")
                continue
            for field in required_fields:
                if field not in item:
                    issues.append(f"{list_key}[{i}].{field} is missing")
                elif item[field] is None or item[field] == "":
                    issues.append(f"{list_key}[{i}].{field} is null/empty")

    return issues


# ---------------------------------------------------------------------------
# Range check — token-based rule matching on full leaf paths
# ---------------------------------------------------------------------------

# Each rule is (keyword_tokens, (min, max)).
#  - keyword_tokens: all of these must appear (as tokens) in the leaf's effective
#    key set for the rule to apply.
#  - The effective key set is built from the leaf's own key plus (if the leaf
#    is a boundary word like 'min'/'max'/'value') its parent's key.
# Examples:
#   leaf path stages[0].kc              → tokens {kc}              → rule ["kc"]            matches
#   leaf path stages[0].ndvi_range.min  → tokens {min, ndvi, range} → rule ["ndvi"]          matches
#   leaf path general_rules[2]          → tokens {general, rules}  → no rules match         skip
_RANGE_RULES: list[tuple[list[str], tuple[float, float]]] = [
    (["ndvi"],                (-1.0, 1.0)),
    (["kc"],                  (0.1, 1.5)),
    (["crop", "coefficient"], (0.1, 1.5)),
    (["mad"],                 (0.0, 1.0)),
    (["depletion"],           (0.0, 1.0)),
    (["das"],                 (0, 365)),
    (["root", "depth", "mm"], (0, 3000)),
    (["harvest", "index"],    (0.1, 0.8)),
    (["confidence"],          (0.0, 1.0)),
    (["ph"],                  (0.0, 14.0)),
    (["season", "days"],      (1, 400)),
    (["total", "days"],       (1, 400)),
]

_BOUNDARY_LEAVES = {"min", "max", "value", "lower", "upper", "from", "to"}


def _tokenize(s: str) -> set[str]:
    return {t.lower() for t in re.split(r"[^a-zA-Z0-9]+", s) if t}


def _effective_tokens(path_segments: list[str]) -> set[str]:
    if not path_segments:
        return set()
    leaf = path_segments[-1]
    tokens = _tokenize(leaf)
    if leaf in _BOUNDARY_LEAVES and len(path_segments) >= 2:
        tokens |= _tokenize(path_segments[-2])
    return tokens


def _check_ranges(doc: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for path_segments, value in _walk_leaves(doc):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue

        effective = _effective_tokens(path_segments)
        for rule_tokens, (lo, hi) in _RANGE_RULES:
            if all(tok in effective for tok in rule_tokens):
                if numeric < lo or numeric > hi:
                    issues.append(
                        f"{_fmt_path(path_segments)}={value} out of range "
                        f"(expected {lo}..{hi})"
                    )
                # One rule match is enough — avoid double-flagging
                break
    return issues


def _walk_leaves(obj: Any, path: list[str] | None = None) -> Iterable[tuple[list[str], Any]]:
    """Yield (path_segments, leaf_value) for every non-container leaf."""
    if path is None:
        path = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_leaves(v, path + [str(k)])
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            # Carry the parent key; index annotation is only for display
            yield from _walk_leaves(item, path + [f"[{i}]"])
    else:
        yield (path, obj)


def _fmt_path(segments: list[str]) -> str:
    out = ""
    for s in segments:
        if s.startswith("["):
            out += s
        else:
            out += f".{s}" if out else s
    return out or "<root>"


# ---------------------------------------------------------------------------
# Logical check — doc_type specific rules
# ---------------------------------------------------------------------------


def _check_logical(doc: dict[str, Any], classification: Classification) -> list[str]:
    if classification.doc_type == DocType.STAGE_DEFINITION:
        return _check_stage_continuity(doc)
    if classification.doc_type == DocType.IRRIGATION_PARAMETERS:
        return _check_stage_codes_unique(doc)
    return []


def _check_stage_continuity(doc: dict[str, Any]) -> list[str]:
    stages = doc.get("stages")
    if not isinstance(stages, list) or len(stages) < 2:
        return []

    try:
        parsed = [
            (s.get("stage_code", f"#{i}"), int(s["start_das"]), int(s["end_das"]))
            for i, s in enumerate(stages)
        ]
    except (KeyError, TypeError, ValueError):
        # Structure check already reports missing/non-numeric stage bounds.
        return []

    issues: list[str] = []
    for code, start, end in parsed:
        if start > end:
            issues.append(f"stage {code} has start_das={start} > end_das={end}")

    ordered = sorted(parsed, key=lambda t: (t[1], t[2]))
    for (prev_code, _, prev_end), (curr_code, curr_start, _) in zip(ordered, ordered[1:]):
        if curr_start <= prev_end:
            issues.append(
                f"stages {prev_code} and {curr_code} overlap "
                f"(DAS {curr_start} is inside {prev_code}'s range ending {prev_end})"
            )
        elif curr_start > prev_end + 1:
            issues.append(
                f"gap between {prev_code} and {curr_code}: "
                f"no stage covers DAS {prev_end + 1}..{curr_start - 1}"
            )
    return issues


def _check_stage_codes_unique(doc: dict[str, Any]) -> list[str]:
    stages = doc.get("stages")
    if not isinstance(stages, list):
        return []
    codes: list[str] = []
    for i, s in enumerate(stages):
        if isinstance(s, dict) and s.get("stage_code"):
            codes.append(str(s["stage_code"]))
    dupes = [c for c in set(codes) if codes.count(c) > 1]
    if dupes:
        return [f"duplicate stage_code(s): {sorted(dupes)}"]
    return []
