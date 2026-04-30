"""
Tiny Phase 1 retrieval eval.

WHY this exists at all (Phase 1):
  Without a baseline, every change to ingestion, metadata, or retrieval
  silently regresses recall. Even 5–8 cases catch the obvious classes of
  break (wrong collection, missing is_active filter, common_collection not
  consulted, doc_key drift). Phase 2 will expand to per-engine query sets.

WHY assertion is "expected ⊆ retrieved" (subset, not equality):
  Retrieval is allowed to return MORE docs than expected (e.g. when extra
  knowledge has been ingested for the same engine). The eval only fails when
  an expected doc_key is MISSING — that's a true regression. New docs are
  not regressions.

WHY a case can have expected_doc_keys = [] :
  Verifies the negative case: a crop with no docs for that engine must
  return zero results, not crash and not borrow from another crop.

How to run:
  conda run -n agri pytest eval/test_retrieval.py -v
"""

import json
import sys
from pathlib import Path

# Allow `python eval/test_retrieval.py` from the repo root without installing
# the project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.retrieval import retrieve  # noqa: E402

try:
    import pytest  # type: ignore
    _HAS_PYTEST = True
except ImportError:
    # Eval must be runnable without dev dependencies — `python eval/test_retrieval.py`
    # falls through to a plain script runner below. CI/dev installs pytest.
    _HAS_PYTEST = False

CASES_PATH = Path(__file__).parent / "cases.json"


def _load_cases():
    data = json.loads(CASES_PATH.read_text())
    return data["cases"]


def _run_case(case):
    docs = retrieve(crop=case["crop"], engine=case["engine"], k=10)
    retrieved_keys = {d["doc_key"] for d in docs}
    expected = set(case["expected_doc_keys"])

    missing = expected - retrieved_keys
    assert not missing, (
        f"Case {case['id']}: missing expected doc_keys {missing}. "
        f"Retrieved: {sorted(retrieved_keys)}"
    )

    if not expected:
        # Negative case: no docs are expected, so none should be returned.
        assert not retrieved_keys, (
            f"Case {case['id']}: expected zero docs but got {sorted(retrieved_keys)}"
        )


if _HAS_PYTEST:
    @pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
    def test_retrieval(case):
        _run_case(case)


def _main():
    """
    Plain-script runner for environments without pytest.

    NOTE: Cases describe the EXPECTED state of the DB once `dummy_data/` has
    been ingested through the /upload flow. A "missing" failure on a fresh DB
    is a data-loading gap, not a code bug — ingest the documents first, then
    rerun. The negative case (`wheat_stage_unknown`) is the smoke test that
    proves retrieval plumbing works regardless of ingestion state.
    """
    cases = _load_cases()
    failures = []
    for case in cases:
        try:
            _run_case(case)
            print(f"  PASS  {case['id']}")
        except AssertionError as exc:
            print(f"  FAIL  {case['id']}: {exc}")
            failures.append(case["id"])
    print(f"\n{len(cases) - len(failures)}/{len(cases)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    _main()
