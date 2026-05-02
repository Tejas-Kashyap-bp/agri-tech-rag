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

E6 (financial) was removed from the apple build — the UI does not surface a
financial card and removing the engine cuts one full LLM round-trip from the
critical path. Do not reuse the e6 id for a different engine.
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
    # Which engine bucket to read from at retrieval time. Defaults to
    # engine_id (1-to-1 mapping). Override when two engines should share the
    # same ingested doc pool — e.g. E4.1 (risk) and E4.2 (cure) both read
    # from the e4_pest_disease_risk bucket so one ingestion of the
    # ipm_schedule + pest_disease_condition_rule docs serves both.
    retrieve_engine_id: str = ""


ENGINES: list[EngineSpec] = [
    EngineSpec(
        engine_id="e1_stage",
        output_key="stage",
        focus=(
            "Determine the current crop growth stage from the stage definition "
            "document, then ADJUST that stage for the orchard's altitude. "
            "Apple is grown across a wide altitude band in the Himalayan belt "
            "and phenology shifts LATER as altitude rises — roughly 10–15 days "
            "of delay for every additional 1,000 ft of elevation. For example, "
            "at ~5,000 ft full bloom may fall around mid-May; at ~6,000 ft the "
            "same Flowering stage typically lands around end-May; at ~7,000 ft "
            "it shifts to mid-June. Decide the altitude to use as follows: "
            "if extra.altitude_ft is present, use that value verbatim; "
            "otherwise INFER a typical altitude from the orchard's location "
            "(extra.location.district / state / country) using your knowledge "
            "of the apple-growing belts of Himachal Pradesh, Jammu & Kashmir, "
            "Uttarakhand, etc. — for example Shimla / Kullu / Mandi / Kinnaur "
            "valley-floor orchards sit around 4,500–6,500 ft, while upper "
            "Kinnaur, Lahaul, and high-belt Kashmir orchards run 7,000–9,000 ft. "
            "State the altitude you used and whether it was provided or "
            "inferred from the place name. Then take the document's calendar "
            "windows as the lower-altitude (~5,000 ft) baseline and shift the "
            "calendar→stage mapping accordingly. If two stages straddle the "
            "adjusted date, pick the one the orchard has clearly entered. "
            "Frame the summary around the CALENDAR DATE, not the moment the "
            "farmer is reading it. Advisories may be consumed a day or two "
            "after generation, so DO NOT say 'today' / 'right now'. Phrase it "
            "as the stage the orchard is in around current_date — for example: "
            "'For an orchard in Shimla district (around 6,500 ft, inferred "
            "from the location), the apple crop is currently in the Flowering "
            "stage (S2) around early May — about a couple of weeks later than "
            "lower-altitude orchards would be at the same calendar date.' "
            "Always state the altitude used (and whether it was provided or "
            "inferred) and call out that the stage is shifted because of "
            "altitude. Write in simple, plain language a "
            "farmer can read aloud. Do NOT mention NDVI, NDRE, EVI, or any "
            "satellite index in the farmer-facing summary, even if such data "
            "is in the context."
        ),
        prompt_version="v2",
    ),
    # NOTE: e2_irrigation removed for apple — perennial tree crops do not run
    # the daily-irrigation advisory in this system. Engine slot intentionally
    # left empty; do not reuse the e2 id for a different engine.
    EngineSpec(
        engine_id="e3_nutrition",
        output_key="fertilizer",
        focus=(
            "Recommend a fertilizer / nutrient action for the orchard around "
            "current_date. If the context indicates a pre-sowing situation, "
            "give a basal-dose recommendation. Otherwise look up the closest "
            "scheduled day in the fertigation schedule and recommend that "
            "step, adjusted qualitatively for soil (SOC, pH) and recent "
            "field-condition signals when those are present. "
            "Write in simple, plain language for a farmer. Avoid 'today' / "
            "'right now' wording — say 'around this time' or 'in the coming "
            "days' since the advisory may be read a day or two later. Do NOT "
            "mention NDVI, NDRE, EVI, or any satellite index in the "
            "farmer-facing summary."
        ),
    ),
    EngineSpec(
        engine_id="e4_pest_disease_risk",
        output_key="pest_disease_risk",
        focus=(
            "Real-time pest & disease risk prediction driven by weather. For the "
            "current crop stage, evaluate every rule in the pest_disease_condition_rule "
            "document (covering both pests and diseases) against the live weather "
            "snapshot — temperature band, humidity band, and conducive-condition "
            "duration (leaf wetness / sustained warm-humid window). Report the "
            "triggered organisms with their base_risk_pct, the near-miss organisms "
            "(any one band only marginally outside), and a short qualitative summary "
            "of which agronomic factor (temperature, humidity, duration) is driving "
            "the risk today. Do NOT recommend specific pesticide/fungicide products — "
            "spray-plan / cure recommendations are out of scope for this engine and "
            "are owned by the upcoming IPM-aligned cure-schedule engine."
        ),
    ),
    EngineSpec(
        engine_id="e4_2_pest_disease_cure",
        output_key="pest_disease_cure",
        retrieve_engine_id="e4_pest_disease_risk",
        focus=(
            "IPM-aligned cure & spray plan for today, scaled to THIS farm. Inputs "
            "you must use: extra.tree_count, extra.farm_area_acres, current_date, "
            "and (when present) the upstream pest_disease_risk output's list of "
            "triggered_organisms. From the retrieved ipm_schedule document:\n"
            "  1. Pick the stage block whose [month_start_mm_dd, month_end_mm_dd] "
            "     window contains current_date (mm-dd compare). If today falls "
            "     between two windows, use the one starting earliest.\n"
            "  2. If pest_disease_risk has triggered_organisms, prioritise the "
            "     entries in that block whose `targets` list intersects the "
            "     triggered organism names. Otherwise list the full preventive "
            "     block for that stage.\n"
            "  3. Compute total_spray_volume_l = tree_count * "
            "     scaling_basis.default_spray_volume_per_tree_l.\n"
            "  4. For EACH organic and chemical line, compute the farmer-specific "
            "     quantity using the rule:\n"
            "       basis='spray_solution' → qty = concentration * "
            "       (total_spray_volume_l / rate_basis_volume_l). Sum together "
            "       water volume = total_spray_volume_l litres.\n"
            "       basis='per_acre'       → qty = concentration * farm_area_acres.\n"
            "       basis='per_tree'       → qty = concentration * tree_count.\n"
            "       basis='guidance_only' or 'as_needed' → emit the action text "
            "       verbatim, no quantity.\n"
            "     If concentration_min and concentration_max are present, compute "
            "     the qty range using both. Always show units.\n"
            "  5. Return TWO recommendation lists: organic_recommendations[] and "
            "     chemical_recommendations[]. Each item must include: material (or "
            "     action), computed_qty (as a string with units, or null for "
            "     guidance lines), per_100l_or_per_acre_basis (the original CSV "
            "     line, for traceability), and targets (if present).\n"
            "  6. The summary must lead with the calendar stage and tree count "
            "     (e.g., 'For your 200-tree orchard in the Petal Fall stage, "
            "     mix 5 kg Mancozeb in 2,000 L water…')."
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
]
