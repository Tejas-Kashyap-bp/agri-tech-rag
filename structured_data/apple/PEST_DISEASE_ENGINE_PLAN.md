# Apple pest & disease engines — design notes

Two logically distinct engines under the "pest & disease" umbrella for apple:

## Engine 4.1 — `e4_pest_disease_risk` (BUILT, this commit)

**Purpose:** real-time, weather-driven pest & disease *risk prediction*. Tells the farmer *what is likely to attack right now* given today's temperature, humidity, and conducive-condition duration.

- Engine id: `e4_pest_disease_risk`
- Output key: `pest_disease_risk`
- Data source: `structured_data/apple/apple_pest_disease_condition_rule.json`
  - One combined doc covering both pests (10 organisms × 4 stages = 40 rules) and diseases (7 organisms × 4 stages = 28 rules) → **68 rules total**.
  - Built from `agri-apple-data/Master data Apple Insect pest.xlsx` + `agri-apple-data/Master data Apple Diseases.xlsx`.
- Doc type: `pest_disease_condition_rule` (existing pipeline doc_type, lenient validator).
- Stage codes used: `S1`–`S4` (Vegetative, Flowering, Fruiting, Harvesting). `S5` Dormancy has no entries — tree is leafless, no active pest/disease pressure (matches the stage-doc convention).
- Evaluation logic (encoded in the JSON `evaluation_logic` field): a rule is "triggered" when current temperature, humidity, AND conducive duration all sit inside the rule's bands; the engine returns triggered organisms with `base_risk_pct`, plus near-misses where exactly one band is marginally outside.
- **Out of scope:** product/dose recommendations. Cure / spray plan belongs to engine 4.2 (below).

## Engine 4.2 — IPM-aligned cure / spray schedule (DEFERRED — no data yet)

**Purpose:** schedule-based *cure* / *spray plan* — given a triggered pest or disease (or a date in the IPM calendar), recommend the specific spray / treatment / dose.

- Provisional engine id (do not wire up yet): `e7_pest_disease_cure` (or whatever fits the final numbering — the `e2` slot stays vacated, do not reuse it).
- Provisional output key: `pest_disease_cure`.
- Provisional doc type: `ipm_schedule` (already exists in the pipeline).
- **Why deferred:** client has not provided the IPM-aligned spray schedule for apple. The Maize IPM-style "T+n days" schedule is **not applicable** to apple — apple is perennial and IPM events should be keyed on calendar dates / phenological stages, not days-after-sowing. Mirrors the fertigation engine's switch from DAS to absolute calendar dates.
- **Resume instructions:**
  1. Get the apple IPM data from client (calendar-keyed list of: stage / date window, pest_or_disease target, treatment / product / dose).
  2. Decide the data shape — likely `events[]` with `{stage_code, calendar_window, organism_target, treatment, dose, application_method}`. Reuse the same `S1..S5` stage codes as the stage doc.
  3. Add a new `EngineSpec` to `app/advisory/engines.py` (placed after `e6_financial` so existing slots are unchanged), update `_DEPENDENCIES` in `app/advisory/orchestrator.py`, add a route entry in `app/api/routes/advisory.py`, and add the engine value to the `Engine` enum in `app/schemas.py`.
  4. The new engine should depend on E1 (stage) and E4 (pest_disease_risk) — the cure plan reads which organisms are at-risk now and produces the matching treatment.

## Engine slot summary (post-edit)

| Slot | Engine id | Status |
|---|---|---|
| E1 | `e1_stage` | active |
| E2 | — | **removed** (irrigation, not applicable to perennial apple) |
| E3 | `e3_nutrition` | active (fertigation engine — see `FERTIGATION_CHECKPOINT.md`) |
| E4 | `e4_pest_disease_risk` | **active (this commit)** |
| E5 | `e5_yield` | active |
| E6 | `e6_financial` | active |
| (future) | `pest_disease_cure` (4.2 above) | deferred — awaiting client IPM data |
