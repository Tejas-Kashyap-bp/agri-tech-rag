"""
Multi-engine advisory orchestrator.

Runs E1, E3, E4.1, E4.2, E5 for one farm context and assembles the
structured response shape (E2 irrigation and E6 financial are intentionally
omitted for the apple build):

    {
      "request_id":          "<uuid>",
      "context":             {...echo of inputs...},
      "stage":               {...},   # E1
      "fertilizer":          {...},   # E3
      "pest_disease_risk":   {...},   # E4.1
      "yield":               {...},   # E5
      "pest_disease_cure":   {...}    # E4.2
    }

Execution tiers (driven by inter-engine data dependencies):

    Tier 1:  E1 (stage)                            → sequential
    Tier 2:  E3 + E4 + E5                          → parallel (asyncio.gather)
             each receives E1's output as upstream
    Tier 3:  E6 (financial)                        → sequential
             receives E5's output as upstream

WHY this shape (not all-parallel, not all-sequential):
  - E3-E5 all need to know the current growth stage to pick the right
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
from typing import Any, Optional

from app.advisory.context import AdvisoryContext
from app.advisory.engines import ENGINES, EngineSpec
from app.advisory.generator import generate_for_engine
from app.advisory.satellite_layer import (
    build_satellite_advisory,
    build_satellite_summary,
    classify as classify_satellite,
)
from app.advisory.yield_layer import (
    build_satellite_advisory as build_yield_satellite_advisory,
    build_yield_summary,
    build_total_yield_summary,
    classify_yield_satellite,
    compute_adjustment_percent,
    compute_base_yield,
    compute_final_yield,
)

log = logging.getLogger("advisory.orchestrator")

# Per-engine LLM call budget. Halved from 45s because generator.py makes up
# to TWO LLM calls per engine (initial + schema retry), each with this
# timeout — so the engine's worst-case wall clock is 2 × this value. With
# 22.5s, worst case = 45s, which matches the per-tier budget assumed below.
PER_ENGINE_TIMEOUT_S: float = 90.0
# Raised from 360s. With Sentinel Hub now pre-fetched ONCE before Tier 1
# (instead of E3 and E5 each walking ~150 windows in parallel), the SH cost
# is paid once, so the per-tier worst case is ~180s × 3 tiers = 540s. 600s
# gives headroom so E4.2 (Tier 3) reliably starts.
REQUEST_DEADLINE_S: float = 600.0

# Single source of truth for inter-engine dependencies. Both
# generate_advisories (tier-parallel path) and generate_single (per-engine
# endpoint path) read from this map so adding a new engine is one edit.
_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "e1_stage": (),
    "e3_nutrition": ("e1_stage",),
    "e4_pest_disease_risk": ("e1_stage",),
    "e4_2_pest_disease_cure": ("e1_stage", "e4_pest_disease_risk"),
    "e5_yield": ("e1_stage",),
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
    # Live Sentinel Hub satellite layer — enriches E3 (fertilizer / INM)
    # and E5 (yield correction). Caller-supplied satellite values always
    # win; this is purely additive. A live-fetch failure must NOT break
    # the engine: we trap the exception in the helper and fall through
    # with the original context so the engine still runs (just without
    # the satellite-driven nudges).
    if spec.engine_id in ("e3_nutrition", "e5_yield"):
        context = _enrich_context_with_live_satellite(context)

    inputs_used = _build_inputs_used(context, spec, upstream_outputs)
    try:
        result = await asyncio.to_thread(
            generate_for_engine,
            context,
            spec,
            k,
            timeout,
            upstream_outputs,
        )
        if spec.engine_id == "e3_nutrition":
            _decorate_with_satellite_advisory(result, context)
            _decorate_with_nutrition_guardrails(result, context)
        if spec.engine_id == "e5_yield":
            _decorate_with_yield_calculation(result, context)
        return {**result, "inputs_used": inputs_used, "status": "ok"}
    except Exception as exc:
        log.exception(
            "engine_error request_id=%s engine=%s",
            request_id, spec.engine_id,
        )
        return {**_error_stub(spec, exc), "inputs_used": inputs_used}


# ---------------------------------------------------------------------------
# inputs_used — deterministic snapshot of what the engine "saw"
# ---------------------------------------------------------------------------
# Built in code (not by the LLM) so the UI can show a "why this advisory"
# drawer that lists the actual driving inputs (current_date, DAS, weather
# bands, soil, satellite, upstream summaries). Computed BEFORE the LLM call
# so it is attached even when the engine errors — the UI can still explain
# what the system tried to reason over.
# ---------------------------------------------------------------------------


_RELEVANT_EXTRA_KEYS_BY_ENGINE: dict[str, tuple[str, ...]] = {
    "e1_stage": (),
    "e3_nutrition": ("farm_area_acres",),
    "e4_pest_disease_risk": ("organism_focus", "tweak_mode"),
    "e4_2_pest_disease_cure": ("tree_count", "farm_area_acres"),
    "e5_yield": ("farm_area_acres", "expected_harvest_date"),
}


def _summarise_satellite(sat: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce satellite timeseries to a 'latest reading' for each band so the
    UI drawer stays compact. The full series is still in the request echo."""
    if not sat:
        return None
    out: dict[str, Any] = {}
    for key in ("ndvi_timeseries", "evi_timeseries", "ndwi_timeseries"):
        series = sat.get(key) or []
        if series:
            last = series[-1]
            band = key.replace("_timeseries", "")
            out[band] = {"date": last.get("date"), "value": last.get("value")}
    return out or None


def _build_inputs_used(
    context: AdvisoryContext,
    spec: EngineSpec,
    upstream_outputs: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "crop": context.crop,
        "current_date": context.current_date.isoformat(),
        "sowing_date": context.sowing_date.isoformat(),
        "days_after_sowing": context.das,
    }
    if context.weather:
        inputs["weather"] = context.weather
    if context.soil:
        inputs["soil"] = context.soil
    sat_summary = _summarise_satellite(context.satellite)
    if sat_summary:
        inputs["satellite_latest"] = sat_summary
    if context.detection:
        inputs["detection"] = context.detection

    relevant_keys = _RELEVANT_EXTRA_KEYS_BY_ENGINE.get(spec.engine_id, ())
    if context.extra and relevant_keys:
        picked = {k: context.extra[k] for k in relevant_keys if k in context.extra}
        if picked:
            inputs["farm"] = picked

    if upstream_outputs:
        inputs["upstream_summaries"] = {
            up_id: (up.get("summary") or "")[:280]
            for up_id, up in sorted(upstream_outputs.items())
        }

    return inputs


async def generate_advisories(context: AdvisoryContext, k: int = 1) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    log.info(
        "advisory_start request_id=%s crop=%s das=%d k=%d",
        request_id, context.crop, context.das, k,
    )

    advisories: dict[str, Any] = {}

    # ---- Tier 0: Sentinel Hub satellite pre-fetch -------------------------
    # E3 (nutrition) and E5 (yield) both consume the same NDVI / NDRE / EVI
    # block. Previously each engine called _enrich_context_with_live_satellite
    # itself, so the ~150-window SH walk ran twice in parallel — racing the
    # SH trial rate limit and inflating wall-clock. Doing it ONCE up front
    # (with the idempotency guard inside _enrich_context_with_live_satellite)
    # turns the second call into a no-op and frees Tier 2 budget for the LLM
    # round-trips. Failures are already swallowed inside the helper, so a
    # broken SH call still lets the engines run with whatever satellite
    # values the caller passed.
    context = await asyncio.to_thread(_enrich_context_with_live_satellite, context)

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
    tier2_ids = ["e3_nutrition", "e4_pest_disease_risk", "e5_yield"]
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

    # ---- Tier 3: E4.2 (IPM cure) — needs tier-2 ---------------------------
    # E4.2 reads E4.1's triggered_organisms. E6 (financial) was removed from
    # the apple build, so this tier now contains a single engine.
    e42_spec = _spec_by_id("e4_2_pest_disease_cure")
    tier3_specs = [e42_spec]
    upstream_for_tier3 = {
        "e1_stage": e1_output,
        "e4_pest_disease_risk": advisories["pest_disease_risk"],
        "e5_yield": advisories["yield"],
    }
    tier3_remaining = REQUEST_DEADLINE_S - (time.monotonic() - started)
    if tier3_remaining <= 0:
        for s in tier3_specs:
            advisories[s.output_key] = _deadline_stub(s)
    else:
        per_engine = min(PER_ENGINE_TIMEOUT_S, tier3_remaining)
        tier3_results = await asyncio.gather(
            *[
                _run_engine(
                    context, s,
                    k=k,
                    timeout=per_engine,
                    upstream_outputs=upstream_for_tier3,
                    request_id=request_id,
                )
                for s in tier3_specs
            ],
            return_exceptions=False,
        )
        for s, r in zip(tier3_specs, tier3_results):
            advisories[s.output_key] = r

    # ---- Guardrail decoration: add deterministic guardrails to E4.1 + E4.2 ----
    _decorate_with_guardrails(
        e41_result=advisories.get("pest_disease_risk", {}),
        e42_result=advisories.get("pest_disease_cure", {}),
        context=context,
        e1_output=e1_output,
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


def _decorate_with_nutrition_guardrails(
    e3_result: dict[str, Any],
    context: AdvisoryContext,
) -> None:
    """Add nutrition guardrails to E3 fertilizer result in-place. Never raises."""
    try:
        from app.advisory.nutrition_guardrails import decorate_with_nutrition_guardrails
        decorate_with_nutrition_guardrails(
            e3_result=e3_result,
            weather=context.weather,
            context_extra=context.extra,
        )
    except Exception:
        log.warning("nutrition_guardrail_decorate_failed", exc_info=True)


def _decorate_with_guardrails(
    e41_result: dict[str, Any],
    e42_result: dict[str, Any],
    context: AdvisoryContext,
    e1_output: dict[str, Any] | None,
) -> None:
    """Add all 7 guardrails to E4.1 and E4.2 in-place. Never raises."""
    try:
        from app.advisory.guardrails import decorate_with_guardrails
        e1_summary = (e1_output or {}).get("summary") or None
        # Merge context.satellite (typed slot) into extra so the LAI extractor
        # in the guardrail can see lai_current / ndvi_current. Without this
        # merge the guardrail only sees extra.satellite, which production
        # /farm-advisory does not populate (live SH lands in the typed slot).
        merged_extra: dict[str, Any] = dict(context.extra or {})
        if context.satellite:
            existing_sat = merged_extra.get("satellite")
            if isinstance(existing_sat, dict):
                merged_extra["satellite"] = {**context.satellite, **existing_sat}
            else:
                merged_extra["satellite"] = dict(context.satellite)
        decorate_with_guardrails(
            e41_result=e41_result,
            e42_result=e42_result,
            weather=context.weather,
            context_extra=merged_extra,
            current_date=context.current_date,
            e1_summary=e1_summary,
        )
    except Exception:
        log.warning("guardrail_decorate_failed", exc_info=True)


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


def _enrich_context_with_live_satellite(context: AdvisoryContext) -> AdvisoryContext:
    """Enrich context.satellite with a live Sentinel Hub reading
    (ndvi_current, ndvi_delta_7d, ndre_current, evi_current). Caller-supplied
    keys win — `setdefault` only fills gaps. Wrapped in try/except so a
    Sentinel Hub or geometry failure cannot break the engine path; on failure
    the engine continues with whatever satellite data the caller passed (or
    none, in which case the engine simply skips satellite-derived nudges)."""
    try:
        # Idempotency guard: if a prior step already populated context.satellite
        # with a live SH reading, do not re-fetch. This lets generate_advisories
        # pre-fetch ONCE before Tier 1 and have E3 / E5 reuse the result instead
        # of each launching their own ~150-window walk against the rate-limited
        # SH trial.
        if (context.satellite or {}).get("source") == "sentinel-hub":
            return context

        from app.data_fetchers.satellite_live import get_satellite_data

        extra = context.extra or {}
        live = get_satellite_data(
            sowing_date=context.sowing_date,
            farm_polygon=extra.get("farm_polygon"),
            latitude=(extra.get("location") or {}).get("latitude"),
            longitude=(extra.get("location") or {}).get("longitude"),
            farm_area_m2=extra.get("farm_area_m2"),
            farm_area_acres=extra.get("farm_area_acres"),
        )
        merged: dict[str, Any] = dict(context.satellite or {})
        for key, value in live.items():
            merged.setdefault(key, value)
        return context.model_copy(update={"satellite": merged})
    except Exception:
        log.warning("satellite_live_fetch_failed", exc_info=True)
        return context


def _decorate_with_satellite_advisory(
    result: dict[str, Any], context: AdvisoryContext
) -> None:
    """Add satellite-derived fields to an E3 result in-place. Additive only:
    never touches existing keys. Silent no-op on any failure."""
    try:
        if result.get("parse_status") == "error":
            return
        sat = context.satellite or {}
        features = classify_satellite(sat)
        if not features:
            return
        details = result.get("details")
        if not isinstance(details, dict):
            details = {}
            result["details"] = details
        # NOTE: index buckets and raw NDVI / NDRE numbers are intentionally
        # NOT exposed here anymore — the farmer-facing UI must not mention
        # any technical index. Keep the structured fields only under a
        # private `_satellite_debug` slot so internal tooling can still see
        # them for debugging.
        details.setdefault(
            "_satellite_debug",
            {
                "ndvi_health": features["ndvi_health"],
                "ndvi_trend": features["ndvi_trend"],
                "ndre_status": features["ndre_status"],
                "ndvi_current": sat.get("ndvi_current"),
                "ndvi_delta_7d": sat.get("ndvi_delta_7d"),
                "ndre_current": sat.get("ndre_current"),
                "lai_current": sat.get("lai_current"),
                "source": sat.get("source", "unknown"),
            },
        )
        # Integrate the satellite-driven recommendation into the main
        # fertilizer summary so the UI does not need a separate
        # "🛰 satellite advisory" block. Field team feedback: farmers should
        # see one combined fertilizer message, not two.
        sat_text = build_satellite_advisory(features)
        existing_summary = (result.get("summary") or "").strip()
        if sat_text and sat_text not in existing_summary:
            result["summary"] = (
                f"{existing_summary} {sat_text}".strip()
                if existing_summary else sat_text
            )
        # Kept for backward-compatible API consumers but no longer rendered
        # by the farmer UI.
        result.setdefault("satellite_advisory", sat_text)
        result.setdefault("satellite_summary", build_satellite_summary(features))
    except Exception:
        log.warning("satellite_decorate_failed engine=e3_nutrition", exc_info=True)


# ---------------------------------------------------------------------------
# E5 yield calculation decoration
# ---------------------------------------------------------------------------
# Additive layer that runs AFTER the LLM. It computes a deterministic base
# yield from tree geometry and applies a satellite-driven correction. Lives
# next to the E3 decorator so both follow the same "never overwrite, never
# raise" contract.
# ---------------------------------------------------------------------------

# Reasonable apple-orchard defaults used when context.extra does not carry
# the geometry inputs. Picked to keep the engine runnable in demos; real
# deployments should pass explicit per-farm values via context.extra.
_E5_DEFAULTS: dict[str, float] = {
    "radius_of_tree": 0.10,            # m (trunk radius)
    "crop_density": 2000.0,            # fruits per unit TCSA
    "average_fruit_weight_g": 150.0,   # g per fruit
}


def _pick_float(extra: dict[str, Any] | None, *keys: str) -> Optional[float]:
    if not extra:
        return None
    for k in keys:
        if k in extra and extra[k] is not None:
            try:
                return float(extra[k])
            except (TypeError, ValueError):
                continue
    return None


def _decorate_with_yield_calculation(
    result: dict[str, Any], context: AdvisoryContext
) -> None:
    """Add base/adjusted/final yield + summary + satellite_advisory to an E5
    result in-place. Additive only: never overwrites existing keys. Silent
    no-op on any failure so the LLM portion of the response is preserved."""
    try:
        if result.get("parse_status") == "error":
            return

        extra = context.extra or {}
        radius = _pick_float(extra, "radius_of_tree", "tree_radius_m") \
            or _E5_DEFAULTS["radius_of_tree"]
        density = _pick_float(extra, "crop_density", "fruit_density") \
            or _E5_DEFAULTS["crop_density"]
        fruit_w = _pick_float(
            extra, "average_fruit_weight_g", "average_fruit_weight_in_grams",
            "fruit_weight_g",
        ) or _E5_DEFAULTS["average_fruit_weight_g"]

        # Per-tree base yield. The geometry-only formula in yield_layer
        # actually produces a kg-per-tree figure (TCSA × density × fruit
        # weight); the historical "kg_per_acre" naming was a misnomer the
        # field team flagged. We keep the old keys populated for backward
        # compatibility but now also expose the correctly named per-tree
        # and total-orchard fields used by the UI.
        base_per_tree = round(compute_base_yield(radius, density, fruit_w), 2)

        sat = context.satellite or {}
        features = classify_yield_satellite(sat)
        if features is not None:
            adjustment_percent = compute_adjustment_percent(features)
            advisory_text = build_yield_satellite_advisory(
                features, adjustment_percent
            )
        else:
            adjustment_percent = 0
            advisory_text = (
                "Recent field condition data is not available for this "
                "orchard. The yield estimate uses the standard calculation "
                "without any field-condition correction."
            )

        final_per_tree = compute_final_yield(base_per_tree, adjustment_percent)

        # Tree count is supplied by the API caller (see farm_advisory.py
        # _DEMO_TREE_COUNTS). Fall back to a conservative default if
        # missing so the engine still returns a sensible total.
        num_trees = (
            _pick_float(extra, "tree_count", "number_of_trees", "num_trees")
            or 109.0
        )
        num_trees = int(round(num_trees))
        total_yield = round(final_per_tree * num_trees, 2)

        per_tree_line = build_yield_summary(
            base_per_tree, adjustment_percent, final_per_tree
        )
        total_line = build_total_yield_summary(
            final_per_tree, num_trees, total_yield
        )
        summary = f"{per_tree_line} {total_line}"

        # New farmer-facing fields.
        result.setdefault("base_yield_kg_per_tree", base_per_tree)
        result.setdefault("final_yield_kg_per_tree", final_per_tree)
        result.setdefault("number_of_trees", num_trees)
        result.setdefault("total_yield_kg", total_yield)

        # Backward-compat fields (same numbers, old per_acre names).
        result.setdefault("base_yield_kg_per_acre", base_per_tree)
        result.setdefault("adjustment_percent", adjustment_percent)
        result.setdefault("final_yield_kg_per_acre", final_per_tree)
        result.setdefault("yield_summary", summary)
        result.setdefault("satellite_advisory", advisory_text)
        # Raw indices kept under a private debug key; never rendered to
        # farmers by the UI.
        result.setdefault(
            "_satellite_debug",
            {
                "ndvi": sat.get("ndvi_current"),
                "ndre": sat.get("ndre_current"),
                "evi": sat.get("evi_current"),
            },
        )
        # E5 is now a deterministic geometry+satellite computation, not an
        # LLM-driven engine, so the "No active knowledge found…" short-circuit
        # message is no longer the right top-line summary for this card.
        # Overwrite `summary` with the yield calculation string whenever the
        # decorator successfully produced one. We intentionally do this
        # AFTER the setdefaults above so the structured fields are still
        # populated; only the human-facing summary line is replaced.
        existing_summary = result.get("summary") or ""
        is_no_knowledge_stub = "No active knowledge" in existing_summary
        if is_no_knowledge_stub or not existing_summary:
            result["summary"] = summary
    except Exception:
        log.warning("yield_decorate_failed engine=e5_yield", exc_info=True)


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
