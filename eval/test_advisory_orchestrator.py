"""
Unit tests for the advisory orchestrator's deadline math.

WHY this exists:
  The 3-tier orchestrator has hardcoded budget arithmetic spread across three
  blocks. If the deadline accounting drifts, tier-3 (E6 financial) is the
  first to silently get skipped — and a missing financial slot looks like
  "no advice produced," not like "we ran out of time." That's a bad failure
  mode to ship without tests.

We use a fake generator that records calls and simulates configurable
elapsed time per engine. The orchestrator's gathering, tier-by-tier
budgeting, and stub-on-deadline logic should all be covered.
"""

import asyncio
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.advisory import orchestrator as adv_orch  # noqa: E402
from app.advisory.context import AdvisoryContext  # noqa: E402


def _make_ctx() -> AdvisoryContext:
    return AdvisoryContext(
        crop="maize",
        sowing_date=date(2026, 1, 1),
        current_date=date(2026, 2, 1),
        weather=None, soil=None, satellite=None,
        extra={},
    )


def test_all_engines_run_when_deadline_is_generous(monkeypatch):
    calls: list[str] = []

    def fake_generate(context, spec, k, timeout, upstream_outputs):
        calls.append(spec.engine_id)
        return {
            "summary": f"ok {spec.engine_id}",
            "details": {"reasoning": "fake"},
            "source_docs": [],
            "parse_status": "ok",
            "prompt_version": spec.prompt_version,
        }

    monkeypatch.setattr(adv_orch, "generate_for_engine", fake_generate)

    result = asyncio.run(adv_orch.generate_advisories(_make_ctx(), k=1))

    # All six engines should have been invoked.
    assert set(calls) == {
        "e1_stage", "e2_irrigation", "e3_nutrition",
        "e4_crop_health", "e5_yield", "e6_financial",
    }
    # Output keys correctly populated.
    for key in ("stage", "irrigation", "fertilizer",
                "crop_protection", "yield", "financial"):
        assert key in result
        assert result[key]["status"] == "ok"


def test_tier3_gets_deadline_stub_when_budget_exhausted(monkeypatch):
    # Force REQUEST_DEADLINE_S so low that by the time tier-2 finishes,
    # there is no budget left for tier-3 (E6).
    monkeypatch.setattr(adv_orch, "REQUEST_DEADLINE_S", 0.05)
    monkeypatch.setattr(adv_orch, "PER_ENGINE_TIMEOUT_S", 0.05)

    def slow_generate(context, spec, k, timeout, upstream_outputs):
        # Simulate enough wall-clock to eat the budget before E6 can start.
        time.sleep(0.06)
        return {
            "summary": "ok", "details": {"reasoning": "fake"},
            "source_docs": [], "parse_status": "ok",
            "prompt_version": spec.prompt_version,
        }

    monkeypatch.setattr(adv_orch, "generate_for_engine", slow_generate)

    result = asyncio.run(adv_orch.generate_advisories(_make_ctx(), k=1))

    # E6 (financial) should have hit the deadline stub.
    fin = result["financial"]
    assert fin["status"] == "error"
    assert fin["error"]["type"] == "DeadlineExceeded"


def test_engine_error_is_isolated_to_its_own_slot(monkeypatch):
    def maybe_failing(context, spec, k, timeout, upstream_outputs):
        if spec.engine_id == "e3_nutrition":
            raise RuntimeError("simulated engine failure")
        return {
            "summary": "ok", "details": {"reasoning": "fake"},
            "source_docs": [], "parse_status": "ok",
            "prompt_version": spec.prompt_version,
        }

    monkeypatch.setattr(adv_orch, "generate_for_engine", maybe_failing)

    result = asyncio.run(adv_orch.generate_advisories(_make_ctx(), k=1))

    assert result["fertilizer"]["status"] == "error"
    # Sibling tier-2 engines should still be ok.
    assert result["irrigation"]["status"] == "ok"
    assert result["crop_protection"]["status"] == "ok"
    assert result["yield"]["status"] == "ok"
    # Downstream tier-3 still runs.
    assert result["financial"]["status"] == "ok"
