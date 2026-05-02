"""
Per-engine advisory generation.

For each engine:
  1. Retrieve active knowledge (metadata-only, Phase 1).
  2. Build a prompt that includes the context + retrieved docs.
  3. Call the LLM in JSON mode.
  4. Validate against the minimal output contract; retry once on failure.
  5. Attach `source_docs` (doc_key + version) computed from what we retrieved.

WHY one LLM call per engine (instead of one big call for all 5):
  Spec section 5 allows either "single LLM call OR structured multi-call".
  We pick multi-call because:
    - Each engine sees ONLY the knowledge it needs → smaller prompt, less
      chance of cross-engine reasoning bleed.
    - Engines can be parallelized later without redesign.
    - One bad engine answer doesn't poison the others.

WHY traceability is enforced HERE (not trusted from the LLM):
  Spec sections 6 & 7 mandate `source_docs` for audit. We OVERWRITE the LLM's
  source list with what we actually retrieved (doc_key + version). The LLM
  only saw those docs, so this is the ground truth needed for audit replay.

WHY the output contract is minimal (summary + details):
  The system philosophy is "store knowledge, prompt logic". A rigid per-engine
  schema would force a code change every time we want a new field in the
  advisory. Instead we lock down only what every consumer needs:
    - `summary`     : short string the UI can always render
    - `details`     : free-form object — engine-specific shape, evolves freely
    - `source_docs` : audit trail (doc_key + version), set by us not the LLM
    - `parse_status`: "ok" | "fallback" — caller can detect degraded outputs
"""

import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from app.advisory.context import AdvisoryContext
from app.advisory.engines import EngineSpec
from app.llm.gemini_provider import llm
from app.retrieval import retrieve

log = logging.getLogger("advisory.generator")


class EngineAdvisory(BaseModel):
    """
    Minimal contract every engine output must satisfy.

    `details` is intentionally free-form so engines can evolve their output
    shape without code changes. Only `summary` is structurally guaranteed for
    downstream consumers.
    """
    summary: str
    details: dict[str, Any] = {}


_SYSTEM = """You are an agricultural advisory assistant.

You will be given:
  1. A farm context (crop, dates, optional sensor data).
  2. A focused task describing what advisory to produce.
  3. A set of KNOWLEDGE DOCUMENTS retrieved from the knowledge base.

Rules — follow strictly:
  - Use ONLY the knowledge documents and the context. Do not invent values,
    schedules, thresholds, or treatments that are not in the documents.
  - If the documents do not contain enough information to answer, say so in
    `summary` (e.g. "insufficient knowledge — no irrigation parameters
    document found for this crop") and explain in `details.reasoning`.
  - Return valid JSON only. No prose outside the JSON object.

Output schema (exact top-level keys):
{
  "summary": "<one or two sentences, farmer-readable>",
  "details": {
      "reasoning": "<brief explanation, citing which document(s) drove the answer>",
      "...":       "<any additional engine-specific fields you want to include>"
  }
}
"""

_SYSTEM_RETRY = """You are an agricultural advisory assistant.
Your previous response did not match the required JSON shape.

Return a JSON object with EXACTLY these top-level keys:
  - "summary"  : string (one or two sentences, farmer-readable)
  - "details"  : object (must include a "reasoning" field; other fields are free-form)

No prose outside the JSON. No markdown fences.
"""


def generate_for_engine(
    context: AdvisoryContext,
    spec: EngineSpec,
    k: int = 1,
    timeout: float | None = None,
    upstream_outputs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Run one engine end-to-end.

    `timeout` is the wall-clock budget for the LLM call(s). Retrieval is
    treated as effectively instant (local Chroma); the budget is spent on
    the LLM round-trip(s).

    `upstream_outputs` carries summaries from engines that ran earlier in
    the dependency tier (e.g. E1.stage feeds E2-E5; E5.yield feeds E6).
    """
    t0 = time.monotonic()

    # The retrieval query mixes the engine's task focus with the farm context,
    # so similarity ranking favours docs that actually match BOTH "what we are
    # asked to do" and "what farm we are asked about" (e.g. a stage-specific
    # irrigation doc beats a generic one when DAS lands in that stage).
    query_text = f"{spec.focus}\n\n{context.to_prompt_block()}"
    docs = retrieve(
        crop=context.crop,
        engine=spec.retrieve_engine_id or spec.engine_id,
        query_text=query_text,
        k=k,
    )

    # Source-of-truth audit citations: doc_key + version of every doc the LLM
    # actually saw. Computed from retrieval, not asked from the LLM.
    source_docs = [
        {"doc_key": d["doc_key"], "version": d.get("version")}
        for d in docs
        if d.get("doc_key")
    ]

    # No knowledge → short-circuit. Calling the LLM with zero docs wastes a
    # round-trip and risks hallucination.
    if not docs:
        log.info(
            "engine=%s crop=%s docs=0 short_circuit=no_knowledge",
            spec.engine_id, context.crop,
        )
        return {
            "summary": (
                f"No active knowledge found for engine '{spec.engine_id}' "
                f"and crop '{context.crop}'. Please upload the required documents."
            ),
            "details": {
                "reasoning": (
                    "Retrieval returned 0 active documents. Phase 1 retrieval is "
                    "metadata-only (engine + crop + is_active=true) so this means "
                    "either no document of this engine has been ingested for this "
                    "crop, or all versions are marked inactive."
                ),
            },
            "source_docs": [],
            "parse_status": "ok",
            "prompt_version": spec.prompt_version,
        }

    prompt = _build_prompt(context, spec, docs, upstream_outputs)

    # Attempt 1
    raw = llm.complete_json(prompt=prompt, system=_SYSTEM, timeout=timeout)
    parsed = _validate(raw)
    parse_status = "ok"

    # Attempt 2 — retry once with a tighter prompt if validation failed.
    # Mirrors the extractor.py policy so behavior is consistent across the
    # codebase.
    if parsed is None:
        log.warning(
            "engine=%s crop=%s parse_attempt_1=failed retrying",
            spec.engine_id, context.crop,
        )
        raw_retry = llm.complete_json(prompt=prompt, system=_SYSTEM_RETRY, timeout=timeout)
        parsed = _validate(raw_retry)
        if parsed is None:
            # Soft fallback: surface a generic summary plus the raw text in
            # details so the caller still gets something AND can see what the
            # LLM actually said. parse_status flags the degradation.
            log.error(
                "engine=%s crop=%s parse_attempt_2=failed using_fallback",
                spec.engine_id, context.crop,
            )
            parsed = EngineAdvisory(
                summary=(
                    f"Advisory for '{spec.output_key}' could not be produced in the "
                    f"expected format. Raw model output preserved in details."
                ),
                details={
                    "reasoning": "LLM output failed schema validation twice.",
                    "raw_text": (raw_retry or raw)[:2000],
                },
            )
            parse_status = "fallback"

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "engine=%s crop=%s docs=%d elapsed_ms=%d parse_status=%s",
        spec.engine_id, context.crop, len(docs), elapsed_ms, parse_status,
    )

    return {
        "summary": parsed.summary,
        "details": parsed.details,
        "source_docs": source_docs,
        "parse_status": parse_status,
        "prompt_version": spec.prompt_version,
    }


def _build_prompt(
    context: AdvisoryContext,
    spec: EngineSpec,
    docs: list[dict[str, Any]],
    upstream_outputs: dict[str, dict[str, Any]] | None = None,
) -> str:
    docs_block = "\n\n".join(
        f"--- DOC {i+1} ---\n"
        f"doc_key: {d['doc_key']}\n"
        f"doc_type: {d['doc_type']}\n"
        f"version: {d['version']}\n"
        f"collection: {d['collection']}\n"
        f"description: {d['description']}\n"
        f"content:\n{d['content']}"
        for i, d in enumerate(docs)
    )

    upstream_block = ""
    if upstream_outputs:
        # Pass only summary + details from upstream — full retrieved docs are
        # already in scope for the current engine's own retrieval and should
        # not be duplicated. Deterministic ordering for cache stability.
        parts = []
        for engine_id in sorted(upstream_outputs.keys()):
            up = upstream_outputs[engine_id]
            parts.append(
                f"--- {engine_id} ---\n"
                f"summary: {up.get('summary', '')}\n"
                f"details: {up.get('details', {})}"
            )
        upstream_block = (
            "\nUPSTREAM ENGINE OUTPUTS (already produced — treat as authoritative):\n"
            + "\n\n".join(parts)
            + "\n"
        )

    return (
        f"TASK FOCUS:\n{spec.focus}\n\n"
        f"FARM CONTEXT:\n{context.to_prompt_block()}\n"
        f"{upstream_block}\n"
        f"KNOWLEDGE DOCUMENTS ({len(docs)}):\n{docs_block}\n\n"
        f"Produce the advisory as a JSON object with top-level keys "
        f"'summary' and 'details'. 'details' must include a 'reasoning' field; "
        f"other fields inside 'details' are free-form."
    )


def _validate(raw: str) -> EngineAdvisory | None:
    """
    Parse JSON and validate against the minimal contract.

    Returns None on any failure (bad JSON, missing keys, wrong types). The
    caller decides whether to retry or fall back. Failures are logged at
    DEBUG so prompt-tuning sessions can see what the LLM actually returned
    without spamming INFO.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.debug("validate: json_decode_error: %s | raw_head=%r", exc, (raw or "")[:200])
        return None
    if not isinstance(data, dict):
        log.debug("validate: not_a_dict type=%s", type(data).__name__)
        return None
    try:
        return EngineAdvisory.model_validate(data)
    except ValidationError as exc:
        log.debug("validate: schema_error: %s", exc)
        return None
