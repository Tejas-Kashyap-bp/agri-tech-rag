# Apple fertigation engine — checkpoint

Status: **JSON DRAFTED on 2026-05-01.** `structured_data/apple/apple_fertigation_schedule.json` exists and reflects all locked decisions below. Blocker 1 resolved by client (mid-December → encoded as `date_mm_dd: "12-15"` with `tolerance_days: 7`). Blocker 2 deferred per client preference: ship raw values now, replace whole doc when the soil-test correction calculator arrives.

## Resolved decisions (locked, do not re-litigate)

1. **Dose unit basis: per tree** (entire sheet, basal + in-season, all years).
2. **Soil-test pre-correction:** apple-specific soil-test correction table will be supplied by client later. Hold a slot for it; do not reuse another crop's table.
3. **Basal dose exists in Year 1 ONLY.** Year 2, 3, 4 (and 5) have **no `basal_dose`** — every application across those years is an INM event, including the mid-December one. Terminology is client-confirmed: "basal dose" refers exclusively to the year-1 December application; from year 2 onward, even the December application is INM (Integrated Nutrient Management). The JSON reflects this structurally: only the year-1 entry has a `basal_dose` array; year-2+ entries have `inm_events` only, with the mid-December materials carried as INM events at `date_mm_dd: "12-15"`. Year 1 and Year 2 still have no in-season (Jun–Oct) events; Year 3+ adds the in-season schedule.
4. **Year coverage:** schedule spans **years 1–5**. Apply year-4 numbers to year-5 (client-confirmed: "after 5 years that's good enough"). Do not extrapolate beyond year 5 — return year-5 schedule for any age ≥ 5.
5. **Application method (soil vs. foliar):** **not specified, do not infer.** Leave to farmer interpretation. Do **not** add `application_method` field to JSON. (Reverses my earlier proposal.)
6. **Apr–May (flowering) zero entries:** intentional, no fertigation during bloom. Encode as absence of events; engine should return "no fertigation in flowering window."
7. **Date tolerance:** in-season dates carry a small tolerance window — exact size to be derived from weather-guardrail logic (see #10).
8. **Weather guardrails are in Phase 1 scope** for this engine. Frost / heavy-rain skip-or-delay rules need to be designed and built before fertigation engine ships.
9. **Versioning unit:** the entire schedule is one document (`doc_key = apple_fertigation_schedule`). Any client revision replaces the whole doc. Client informed.
10. **No stage dependency.** Engine keys on `(tree_age_years, current_date)` only. Stage doc and fertigation doc are decoupled.
11. **No DAS / T+n.** Perennial — absolute calendar dates only.

## Open blockers (must clear before drafting JSON)

### Blocker 1 — December basal-dose date
Sheet column header is just "DEC". Need a specific date (or window) in December for the basal application. Affects:
- Dormancy boundary in `apple_stage_definition.json` (currently dormancy = Dec 1 onward).
- Advisory engine triggering — need an exact calendar trigger to compare against `current_date`.

Tried client today, no answer. **Retry tomorrow first thing.**

### Blocker 2 — Nutrient Management calculator for Apple
Client owes us the soil-test → topup-needed correction table (apple-specific). Without it, the basal dose in the JSON will be the raw sheet values with no soil-correction layer. Either:
- (a) ship JSON now with raw values and add a `soil_correction` block when the calculator arrives (whole doc replaced — acceptable per decision #9), or
- (b) wait for the calculator before first ingestion.

Default plan: **(a)**, but confirm with client which they prefer.

## In-season event list (locked, ready to encode once blockers clear)

Year 3 / Year 4 doses (Year 5 = Year 4):

| Date | Material | Y3 | Y4 |
|---|---|---|---|
| 15-Jun | 19:19:19 | 4 kg | 5 kg |
| 25-Jun | 12:61:00 | 4 kg | 5 kg |
| 10-Jul | Calcium nitrate | 4 kg | 5 kg |
| 10-Jul | Boron | 400 g | 500 g |
| 20-Jul | Zinc Sulphate | 500 g | 600 g |
| 30-Jul | Micronutrient mix | 2 kg | 3 kg |
| 10-Aug | 13:00:45 | 5 kg | 6 kg |
| 30-Aug | 00:00:50 | 5 kg | 6 kg |
| 15-Sep | 12:61:00 | 5 kg | 6 kg |
| 30-Sep | 13:00:45 | 5 kg | 6 kg |
| 10-Oct | 19:19:19 | 5 kg | 6 kg |

Year 1 / Year 2: basal dose in December only (table per sheet, already verified row-by-row against client-typed values).

## Resume instructions for tomorrow

1. Get answers to Blocker 1 and Blocker 2 from client.
2. Confirm with user which option (a/b) for Blocker 2.
3. Draft `structured_data/apple/apple_fertigation_schedule.json` with shape roughly:
   - top-level: `crop`, `doc_type: fertigation_schedule`, `dose_unit_basis: "per_tree"`
   - `years[]` keyed by `tree_age_years` (1..5; year 5 references year 4)
   - each year has `basal_dose[]` (only year 1) or `inm_events[]` (year 3+) with `{date_mm_dd, material, dose_value, dose_unit, tolerance_days}`
   - `weather_guardrails` block (frost / heavy-rain skip-or-delay rules) — design pending.
4. Run through pipeline: classifier should land it as `fertigation_schedule`; validator is lenient for that doc_type so structure is flexible, but keep field names stable for downstream extractor source-checks.
