"""
Engine specs for the multi-engine advisory flow.

Each spec declares:
  - engine_id     : matches the Engine enum value used at ingestion time so
                    metadata filtering is exact.
  - output_key    : key under which this engine's advisory appears in the
                    final aggregated JSON (see spec section 6).
  - focus         : short instruction telling the LLM what kind of advisory
                    to generate from the retrieved knowledge.

WHY a flat list of specs (instead of subclassed engine objects):
  The whole point of this system (per the implementation spec) is that LOGIC
  lives in the LLM, not in code. Each engine differs only by which slice of
  knowledge it reads and what answer shape it should return. A class hierarchy
  would just be ceremony around a tuple. If/when an engine needs custom code
  (Phase 2), that engine can graduate to its own module — but Phase 1 doesn't
  need it.

E6 (financial) is included in the default flow at the client's request: every
advisory call should produce a financial risk read alongside the agronomic
engines. E6 depends on E5's yield outlook (revenue projection needs an
expected yield), so it runs LAST in the orchestrator's execution tiers.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineSpec:
    engine_id: str
    output_key: str
    focus: str
    # Bumped manually whenever an engine's prompt (focus / system / template)
    # changes in a way that could shift outputs. Echoed in the engine's
    # response so an audit can tie a stored advisory back to the exact
    # prompt that produced it. Format: "<engine_short>.v<n>".
    prompt_version: str = "v1"


ENGINES: list[EngineSpec] = [
    EngineSpec(
        engine_id="e1_stage",
        output_key="stage",
        focus=(
            "Determine the current crop growth stage. Use the days_after_sowing "
            "value from the context together with the stage definition document. "
            "If satellite NDVI data is available, mention whether it is consistent "
            "with the calendar-based stage."
        ),
    ),
    EngineSpec(
        engine_id="e2_irrigation",
        output_key="irrigation",
        focus=(
            "Decide whether the farm needs irrigation right now, and if so, give "
            "a qualitative recommendation (e.g. 'irrigate today', 'skip — rain "
            "expected'). Use stage-specific Kc / MAD / root-depth values from the "
            "irrigation parameters document. Mention any risk flags (water stress, "
            "heat stress, waterlogging, dry spell) suggested by weather and soil."
        ),
    ),
    EngineSpec(
        engine_id="e3_nutrition",
        output_key="fertilizer",
        focus=(
            "Recommend a fertilizer / nutrient action for today. If the context "
            "indicates a pre-sowing situation, give a basal-dose recommendation. "
            "Otherwise, look up the closest scheduled day in the fertigation "
            "schedule and recommend that step, adjusted qualitatively for soil "
            "(SOC, pH) and NDVI stress when those signals are present."
        ),
    ),
    EngineSpec(
        engine_id="e4_crop_health",
        output_key="crop_protection",
        focus=(
            "Generate a crop protection advisory. If the context contains a "
            "`detection` field, treat this as REACTIVE mode and recommend a "
            "treatment for the named pest/disease using the treatment mapping. "
            "Otherwise, treat this as PREVENTIVE mode: pull from the IPM "
            "schedule for the current DAS window and from any condition rules "
            "that match the weather. Always honor spray guardrails (rain, wind)."
        ),
    ),
    EngineSpec(
        engine_id="e5_yield",
        output_key="yield",
        focus=(
            "Produce a qualitative yield outlook and harvest guidance. Use the "
            "yield_parameters document for the crop-specific harvest index and "
            "biomass assumptions. If satellite indices and weather forecasts are "
            "present, comment on expected harvest window and any risk to yield."
        ),
    ),
    EngineSpec(
        engine_id="e6_financial",
        output_key="financial",
        focus=(
            "Produce a financial risk advisory for this farm. Combine the yield "
            "outlook from the upstream E5 engine with the farm's loan / market "
            "context (outstanding loan, market price, repayment history) and the "
            "financial_policy document from the common knowledge base. Report: "
            "projected harvest value, loan coverage ratio (projected value vs "
            "outstanding loan), a qualitative risk category (low / moderate / "
            "high), and the main drivers of that risk. If yield or loan inputs "
            "are missing, say so explicitly rather than inventing numbers."
        ),
    ),
]
