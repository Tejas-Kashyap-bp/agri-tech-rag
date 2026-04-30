"""
Multi-engine advisory orchestrator.

Runs E1–E6 for one farm context and assembles the structured response shape:

    {
      "request_id":      "<uuid>",
      "context":         {...echo of inputs...},
      "stage":           {...},   # E1
      "irrigation":      {...},   # E2
      "fertilizer":      {...},   # E3
      "crop_protection": {...},   # E4
      "yield":           {...},   # E5
      "financial":       {...}    # E6
    }

Execution tiers (driven by inter-engine data dependencies):

    Tier 1:  E1 (stage)                            → sequential
    Tier 2:  E2 + E3 + E4 + E5                     → parallel (asyncio.gather)
             each receives E1's output as upstream
    Tier 3:  E6 (financial)                        → sequential
             receives E5's output as upstream

WHY this shape (not all-parallel, not all-sequential):
  - E2-E5 all need to know the current growth stage to pick the right
    schedule entry / threshold, so E1 must finish first.
  - E6 (financial) needs the yield outlook from E5 to project harvest value,
    so it must wait for E5.
  - Within Tier 2 the four engines are independent, so we run them in
    parallel via asyncio.gather + asyncio.to_thread (the LLM client is
    sync). Wall-clock ≈ 3 × LLM round-trip instead of 6×.

WHY context echo is included in the response:
  Auditors need to see exactly what the engines were asked about (DAS,
  current_date, weather snapshot). Without this, a stored advisory record is
  not reproducible.

WHY partial failures don't 500 the whole request:
  Engines run independently within their tier. If one raises, the others'
  output is still useful. Each engine's slot in the response carries its own
  `status` field ("ok" or "error") so callers can detect the broken slot
  without losing the rest. Downstream tiers still run even if an upstream
  engine errored — they receive an empty upstream_outputs slot for that
  engine and the LLM is free to flag missing upstream context.

Timeout policy:
  - PER_ENGINE_TIMEOUT_S : LLM round-trip budget for a single engine.
  - REQUEST_DEADLINE_S   : hard wall-clock budget for the whole /advisory
                           call. Sized for the 3-tier worst case
                           (3 × 15s = 45s) with headroom for retry + the
                           parallel tier's slowest engine.
"""

import asyncio
import logging
import time
import uuid
from typing import Any

from app.advisory.context import AdvisoryContext
from app.advisory.engines import ENGINES, EngineSpec
from app.advisory.generator import generate_for_engine

log = logging.getLogger("advisory.orchestrator")

# Per-engine LLM call budget. Halved from 45s because generator.py makes up
# to TWO LLM calls per engine (initial + schema retry), each with this
# timeout — so the engine's worst-case wall clock is 2 × this value. With
# 22.5s, worst case = 45s, which matches the per-tier budget assumed below.
PER_ENGINE_TIMEOUT_S: float = 22.5
REQUEST_DEADLINE_S: float = 180.0

# Single source of truth for inter-engine dependencies. Both
# generate_advisories (tier-parallel path) and generate_single (per-engine
# endpoint path) read from this map so adding a new engine is one edit.
_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "e1_stage": (),
    "e2_irrigation": ("e1_stage",),
    "e3_nutrition": ("e1_stage",),
    "e4_crop_health": ("e1_stage",),
    "e5_yield": ("e1_stage",),
    "e6_financial": ("e1_stage", "e5_yield"),
}


def _spec_by_id(engine_id: str) -> EngineSpec:
    for s in ENGINES:
        if s.engine_id == engine_id:
            return s
    raise KeyError(f"engine spec not found: {engine_id}")


async def _run_engine(
    context: AdvisoryContext,
    spec: EngineSpec,
    k: int,
    timeout: float,
    upstream_outputs: dict[str, dict[str, Any]] | None,
    request_id: str,
) -> dict[str, Any]:
    """
    Wrap a sync engine call in a thread so the orchestrator can gather it.

    A failure here does NOT propagate — we trap the exception and return an
    error stub so the parent gather doesn't tear down sibling engines.
    """
    try:
        result = await asyncio.to_thread(
            generate_for_engine,
            context,
            spec,
            k,
            timeout,
            upstream_outputs,
        )
        return {**result, "status": "ok"}
    except Exception as exc:
        log.exception(
            "engine_error request_id=%s engine=%s",
            request_id, spec.engine_id,
        )
        return _error_stub(spec, exc)


async def generate_advisories(context: AdvisoryContext, k: int = 1) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    log.info(
        "advisory_start request_id=%s crop=%s das=%d k=%d",
        request_id, context.crop, context.das, k,
    )

    advisories: dict[str, Any] = {}

    # ---- Tier 1: E1 (stage) — must finish before E2-E5 can start ----------
    e1_spec = _spec_by_id("e1_stage")
    e1_remaining = REQUEST_DEADLINE_S - (time.monotonic() - started)
    if e1_remaining <= 0:
        advisories[e1_spec.output_key] = _deadline_stub(e1_spec)
    else:
        advisories[e1_spec.output_key] = await _run_engine(
            context, e1_spec,
            k=k,
            timeout=min(PER_ENGINE_TIMEOUT_S, e1_remaining),
            upstream_outputs=None,
            request_id=request_id,
        )

    e1_output = advisories[e1_spec.output_key]
    upstream_for_tier2 = {"e1_stage": e1_output}

    # ---- Tier 2: E2, E3, E4, E5 in parallel -------------------------------
    tier2_ids = ["e2_irrigation", "e3_nutrition", "e4_crop_health", "e5_yield"]
    tier2_specs = [_spec_by_id(eid) for eid in tier2_ids]

    tier2_remaining = REQUEST_DEADLINE_S - (time.monotonic() - started)
    if tier2_remaining <= 0:
        for s in tier2_specs:
            advisories[s.output_key] = _deadline_stub(s)
    else:
        per_engine = min(PER_ENGINE_TIMEOUT_S, tier2_remaining)
        tier2_results = await asyncio.gather(
            *[
                _run_engine(
                    context, s,
                    k=k,
                    timeout=per_engine,
                    upstream_outputs=upstream_for_tier2,
                    request_id=request_id,
                )
                for s in tier2_specs
            ],
            return_exceptions=False,
        )
        for s, r in zip(tier2_specs, tier2_results):
            advisories[s.output_key] = r

    # ---- Tier 3: E6 (financial) — needs E5's yield output -----------------
    e6_spec = _spec_by_id("e6_financial")
    e6_remaining = REQUEST_DEADLINE_S - (time.monotonic() - started)
    if e6_remaining <= 0:
        advisories[e6_spec.output_key] = _deadline_stub(e6_spec)
    else:
        upstream_for_e6 = {"e5_yield": advisories["yield"]}
        advisories[e6_spec.output_key] = await _run_engine(
            context, e6_spec,
            k=k,
            timeout=min(PER_ENGINE_TIMEOUT_S, e6_remaining),
            upstream_outputs=upstream_for_e6,
            request_id=request_id,
        )

    total_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "advisory_done request_id=%s total_ms=%d engines=%d",
        request_id, total_ms, len(advisories),
    )

    return {
        "request_id": request_id,
        "context": {
            "crop": context.crop,
            "sowing_date": context.sowing_date.isoformat(),
            "current_date": context.current_date.isoformat(),
            "days_after_sowing": context.das,
        },
        **advisories,
    }


async def generate_single(
    context: AdvisoryContext,
    engine_id: str,
    k: int = 1,
) -> dict[str, Any]:
    """
    Run ONE engine and return its result, plus any upstream engines it
    depends on (so the per-engine endpoint can stand alone without the
    caller having to know the dependency graph).

    Returns the same per-engine slot shape as generate_advisories, plus an
    `upstream` map echoing the dependency outputs that fed this engine.
    """
    request_id = str(uuid.uuid4())
    spec = _spec_by_id(engine_id)
    upstream: dict[str, dict[str, Any]] = {}
    upstream_payload: dict[str, dict[str, Any]] = {}

    # Dependency rules come from _DEPENDENCIES at module scope — single
    # source of truth shared with generate_advisories.
    deps = _DEPENDENCIES.get(engine_id, ())
    needs_e1 = "e1_stage" in deps
    needs_e5 = "e5_yield" in deps

    if needs_e1:
        e1_spec = _spec_by_id("e1_stage")
        e1_out = await _run_engine(
            context, e1_spec, k=k, timeout=PER_ENGINE_TIMEOUT_S,
            upstream_outputs=None, request_id=request_id,
        )
        upstream["stage"] = e1_out
        upstream_payload["e1_stage"] = e1_out

    if needs_e5:
        e5_spec = _spec_by_id("e5_yield")
        e5_out = await _run_engine(
            context, e5_spec, k=k, timeout=PER_ENGINE_TIMEOUT_S,
            upstream_outputs=upstream_payload, request_id=request_id,
        )
        upstream["yield"] = e5_out
        upstream_payload = {"e5_yield": e5_out}

    # If the requested engine IS one of the upstreams, return the already-run
    # result directly — no need to run it twice.
    if engine_id == "e1_stage" and "stage" in upstream:
        result = upstream["stage"]
    else:
        result = await _run_engine(
            context, spec, k=k, timeout=PER_ENGINE_TIMEOUT_S,
            upstream_outputs=upstream_payload or None,
            request_id=request_id,
        )

    return {
        "request_id": request_id,
        "engine_id": engine_id,
        "context": {
            "crop": context.crop,
            "sowing_date": context.sowing_date.isoformat(),
            "current_date": context.current_date.isoformat(),
            "days_after_sowing": context.das,
        },
        spec.output_key: result,
        "upstream": upstream,
    }


def _error_stub(spec: EngineSpec, exc: Exception) -> dict[str, Any]:
    return {
        "summary": f"Could not produce {spec.output_key} advisory ({type(exc).__name__}).",
        "details": {"reasoning": "Engine raised an unhandled error."},
        "source_docs": [],
        "parse_status": "error",
        "status": "error",
        "error": {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        },
    }


def _deadline_stub(spec: EngineSpec) -> dict[str, Any]:
    return {
        "summary": f"{spec.output_key} advisory was skipped — request deadline exceeded.",
        "details": {"reasoning": "Engine did not start within the request budget."},
        "source_docs": [],
        "parse_status": "error",
        "status": "error",
        "error": {
            "type": "DeadlineExceeded",
            "message": f"request budget {REQUEST_DEADLINE_S}s elapsed before this engine started",
        },
    }
