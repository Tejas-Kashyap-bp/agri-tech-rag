# AGRI-RAG Ingestion Pipeline — Adversarial Test Report

**Date:** 2026-04-24
**Scope:** 28 test documents covering all six required categories
**Target pipeline blocks:** Pre-Processing → Heuristic Pre-Filter → LLM Classification → Structured Extraction → Evidence Checker → Validation (Structure / Range / Logical) → Version Control → Output Routing

> **Post-fix note (2026-04-24):** this report was written BEFORE the three
> stability fixes and the keyword-proximity upgrade were merged. See
> `FINAL_TEST_REPORT.md` for the post-fix readiness assessment. The cases
> that previously passed silently (23, 24) now route to EVIDENCE_REVIEW,
> and the "co-located number" traps (F06, F18 in the final-readiness pack)
> are now correctly rejected.

## Pipeline path legend

- **VALID** — passes all layers, stored in ChromaDB
- **INVALID** — rejected with block-named errors
- **AMBIGUOUS** — classification confidence too low / multi-engine / multi-crop → human confirmation
- **EVIDENCE_REVIEW** — values exist without `<field>_source` → `pending_evidence_review`
- **VERSION_CONFLICT** — `doc_key = {crop}_{type}` already active → pending resolution

## Summary matrix

| # | File | Type | Expected Path | Primary target block |
|---|------|------|---------------|----------------------|
| 01 | `A_adversarial/01_vague_irrigation_wheat.pdf.txt` | PDF | EVIDENCE_REVIEW *or* INVALID | Structured Extraction → Evidence Checker |
| 02 | `A_adversarial/02_implicit_kc_values_maize.pdf.txt` | PDF | EVIDENCE_REVIEW | Structured Extraction (no-inference rule) |
| 03 | `A_adversarial/03_mixed_fertigation_irrigation_sugarcane.pdf.txt` | PDF | AMBIGUOUS | LLM Classification (engine split) |
| 04 | `A_adversarial/04_contradictory_kc_directives.json` | JSON | INVALID *or* EVIDENCE_REVIEW | Validation (note contradicts value) |
| 05 | `B_edge_cases/05_ndvi_exact_boundaries.json` | JSON | VALID | Range Validation (inclusive bounds) |
| 06 | `B_edge_cases/06_ndvi_slightly_outside.json` | JSON | INVALID | Range Validation |
| 07 | `B_edge_cases/07_das_overlap_1day.json` | JSON | INVALID | Logical Validation |
| 08 | `B_edge_cases/08_das_tiny_gap.json` | JSON | INVALID | Logical Validation |
| 09 | `B_edge_cases/09_missing_middle_stage.json` | JSON | INVALID | Logical Validation (gap DAS 41-65) |
| 10 | `B_edge_cases/10_negative_das_and_reversed_range.json` | JSON | INVALID | Range + Logical Validation |
| 11 | `C_structural_traps/11_empty_strings_and_nulls.json` | JSON | INVALID | Structure Validation |
| 12 | `C_structural_traps/12_wrong_data_types.json` | JSON | INVALID | Structure Validation |
| 13 | `C_structural_traps/13_flat_instead_of_nested.json` | JSON | INVALID | Structure Validation (schema shape) |
| 14 | `C_structural_traps/14_deeply_nested_extra_keys.json` | JSON | VALID-with-warnings *or* INVALID | Structure Validation (strict vs lenient) |
| 15 | `C_structural_traps/15_invalid_json_trailing_commas.json` | JSON | INVALID | Pre-Processing (JSON parse) |
| 16 | `D_classification_confusion/16_multi_crop_comparative.pdf.txt` | PDF | AMBIGUOUS | LLM Classification (multi-crop) |
| 17 | `D_classification_confusion/17_no_crop_name.json` | JSON | AMBIGUOUS | LLM Classification (missing crop) |
| 18 | `D_classification_confusion/18_generic_common_schedule.pdf.txt` | PDF | AMBIGUOUS | LLM Classification (generic) |
| 19 | `D_classification_confusion/19_ambiguous_engine_mixed_signals.pdf.txt` | PDF | AMBIGUOUS | LLM Classification (multi-engine) |
| 20 | `E_evidence_attacks/20_values_no_source.json` | JSON | EVIDENCE_REVIEW | Evidence Checker |
| 21 | `E_evidence_attacks/21_implied_values_only.pdf.txt` | PDF | EVIDENCE_REVIEW | Structured Extraction (no-inference) |
| 22 | `E_evidence_attacks/22_contradictory_statements.pdf.txt` | PDF | EVIDENCE_REVIEW *or* AMBIGUOUS | Structured Extraction (which Kc wins?) |
| 23 | `E_evidence_attacks/23_source_text_mismatch.json` | JSON | EVIDENCE_REVIEW *or* INVALID | Evidence Checker (source-text verifier) |
| 24 | `E_evidence_attacks/24_hallucinated_page_refs.json` | JSON | EVIDENCE_REVIEW | Evidence Checker (page existence check) |
| 25 | `F_valid_and_conflict/25_valid_rice_stage_definition.json` | JSON | VALID | End-to-end happy path |
| 26 | `F_valid_and_conflict/26_valid_tomato_irrigation.pdf.txt` | PDF | VALID | End-to-end happy path (PDF route) |
| 27 | `F_valid_and_conflict/27_version_conflict_maize_stage_definition.json` | JSON | VERSION_CONFLICT | Version Control |
| 28 | `F_valid_and_conflict/28_valid_wheat_fertigation.csv` | CSV | VALID | End-to-end happy path (CSV route) |

---

## Detailed test cases

### Category A — Adversarial (vague / implicit / mixed / contradictory)

#### Test Case 01 — Vague irrigation wheat PDF
- **Type:** PDF-style text
- **Expected Outcome:** EVIDENCE_REVIEW (preferred) or INVALID if extractor invents numbers
- **Reason:** Entire document uses qualitative adjectives ("moderate", "adequate", "high", "low") with no numerical Kc, MAD, or root depth. Per the no-inference rule, the extractor must emit `null` for every numeric field and the document should route to `pending_evidence_review` — or, if the LLM rephrases adjectives into numbers, the evidence checker must still catch it because no quoted source string can back a number that was never stated.
- **Weakness exposed:** Whether the extractor obeys "missing values → null" under social pressure from a plausible-looking document. Also probes whether confidence scoring penalizes qualitative-only sources.

#### Test Case 02 — Implicit Kc curve
- **Type:** PDF-style text
- **Expected Outcome:** EVIDENCE_REVIEW
- **Reason:** The document explicitly refuses to publish numeric Kc values ("research has shown wide variation … consult FAO-56"). A well-behaved extractor should emit all Kc values as null. A naive LLM will pattern-match "peak flowering Kc = 1.15" from training priors.
- **Weakness exposed:** Model world-knowledge leakage into structured output.

#### Test Case 03 — Mixed fertigation + irrigation
- **Type:** PDF-style text
- **Expected Outcome:** AMBIGUOUS
- **Reason:** Single document describes both irrigation schedule (mm, Kc, soil tension) and fertigation schedule (N/P/K kg/ha) in an operationally inseparable way. `possible_types` regex should flag both. Classifier confidence should fall below the 90% auto-approve bar and route to human confirmation.
- **Weakness exposed:** Whether classifier over-commits to a single `doc_type` when two are co-present, and whether the system can split the document or requires human disambiguation.

#### Test Case 04 — Contradictory Kc directives
- **Type:** JSON
- **Expected Outcome:** INVALID (preferred) or EVIDENCE_REVIEW
- **Reason:** `notes` says "Kc must NEVER exceed 1.0", `kc = 1.2`, `irrigation_notes` says "Kc remains below 1.0". The document is internally inconsistent. A proper validator should flag cross-field contradictions even if every individual field parses cleanly.
- **Expected Errors:**
  - `logical_validation: stages[0].kc (1.2) contradicts general.notes constraint ("never exceed 1.0")`
  - `logical_validation: stages[0].irrigation_notes contradicts stages[0].kc`
- **Weakness exposed:** Cross-field semantic consistency checking (likely currently absent).

---

### Category B — Edge cases

#### Test Case 05 — NDVI at exact boundaries (-1.0, 1.0)
- **Expected Outcome:** VALID
- **Reason:** NDVI constraint is `[-1, 1]` *inclusive*. Both endpoints must pass. Also exercises a degenerate range `min == max` (bare soil frozen at -1).
- **Weakness exposed:** Off-by-one strict-less-than range checks; rejection of degenerate ranges that are agronomically meaningful.

#### Test Case 06 — NDVI 0.0001 outside bounds
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `range_validation: stages[0].ndvi_range.min (-1.0001) out of range [-1, 1]`
  - `range_validation: stages[1].ndvi_range.max (1.0001) out of range [-1, 1]`
- **Weakness exposed:** Float precision tolerance — a lax validator might allow these.

#### Test Case 07 — DAS overlap by 1 day
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `logical_validation: stages[0].end_das (14) overlaps stages[1].start_das (14) — no overlap allowed`
- **Weakness exposed:** Whether overlap check uses `<=` vs `<`.

#### Test Case 08 — 1-day DAS gap
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `logical_validation: gap between stages[0].end_das (14) and stages[1].start_das (16) — DAS 15 unassigned`
- **Weakness exposed:** Gap detector may only fire on multi-day gaps or may silently interpolate.

#### Test Case 09 — Missing middle stage (flowering absent)
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `logical_validation: gap between stages[1].end_das (40) and stages[2].start_das (66) — DAS 41-65 unassigned`
- **Weakness exposed:** Missing-stage detection — the surrounding stages parse fine so the error is only visible at the whole-document level.

#### Test Case 10 — Negative DAS + reversed range
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `range_validation: stages[0].start_das (-7) must be >= 0`
  - `logical_validation: stages[1].start_das (60) > stages[1].end_das (30) — reversed range`
  - `logical_validation: stages[1].ndvi_range.min (0.6) > stages[1].ndvi_range.max (0.4) — reversed range`
- **Weakness exposed:** Whether range validator checks `min <= max` on both DAS and NDVI pairs, and whether negative DAS (agronomically valid for rice nursery) has a documented policy.

---

### Category C — Structural traps

#### Test Case 11 — Empty strings and nulls for required fields
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `structure_validation: crop is empty string`
  - `structure_validation: stages[0].stage_name is empty string`
  - `structure_validation: stages[0].root_depth_mm expected number, got empty string`
  - `structure_validation: stages[1].kc is null but required`
- **Weakness exposed:** Empty-string-as-missing detection. Many validators treat `""` as a valid string.

#### Test Case 12 — Wrong data types (strings-as-numbers, numbers-as-strings)
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `structure_validation: stages[0].kc expected number, got string "0.4"` (even though it's coercible)
  - `structure_validation: stages[0].mad expected number, got string "fifty percent"`
  - `structure_validation: stages[0].root_depth_mm expected integer, got string "300mm"`
  - `structure_validation: stages[0].irrigation_notes expected string, got array`
  - `structure_validation: stages[1].stage_code expected string, got number 2`
- **Weakness exposed:** Silent type coercion from the LLM or JSON parser.

#### Test Case 13 — Flat NDVI fields instead of nested object + string range
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `structure_validation: stages[0].ndvi_range missing (found flat ndvi_min/ndvi_max instead)`
  - `structure_validation: stages[1].ndvi_range expected object, got string "0.4 to 0.7"`
- **Weakness exposed:** Schema strictness about nested shape `{min, max}`.

#### Test Case 14 — Deep nesting with unexpected keys
- **Expected Outcome:** VALID-with-warnings or INVALID (depending on strictness policy)
- **Reason:** Values are all present and agronomically plausible, but the schema has unexpected keys (`metadata.publisher.institution…`, `extra_unexpected_field`, `sub_sub_stages`, `ndvi_range.confidence`). Additionally `ndvi_range.unit` and `confidence` are extra keys inside the nested object.
- **Weakness exposed:** Whether the schema is "additional properties: false" or lenient; whether extra keys silently propagate into the vector store and corrupt downstream retrieval.

#### Test Case 15 — Invalid JSON (trailing commas)
- **Expected Outcome:** INVALID
- **Expected Errors:**
  - `preprocessing: JSON parse error at line N column M — trailing comma`
- **Weakness exposed:** Whether pre-processing reports the failure cleanly with line/col, or swallows the error and passes an empty dict downstream. Also whether a fallback parser (e.g., JSON5) silently accepts this and hides the problem.

---

### Category D — Classification confusion

#### Test Case 16 — Multi-crop comparative fertigation
- **Expected Outcome:** AMBIGUOUS
- **Reason:** Document explicitly covers both maize and wheat. Regex pre-filter should flag both crops; LLM classifier should return low confidence or `crops = [maize, wheat]`. `doc_key = {crop}_{type}` is undefined when crop is ambiguous.
- **Weakness exposed:** Whether classifier collapses to the first-mentioned crop (maize) and silently drops wheat content, or correctly triggers human confirmation to split into two doc_keys.

#### Test Case 17 — No crop name at all
- **Expected Outcome:** AMBIGUOUS
- **Reason:** Crop field missing entirely. Cannot form `doc_key`.
- **Weakness exposed:** Whether LLM classifier confidently guesses a crop from Kc/rooting profile (wheat-like) vs. routing to human.

#### Test Case 18 — Generic "common kharif cereals" schedule
- **Expected Outcome:** AMBIGUOUS
- **Reason:** Document intentionally avoids naming a specific crop. "Common" schedules are a real operational input and the system must decide whether to reject, ingest under a generic key, or force human assignment.
- **Weakness exposed:** Whether there is a policy for generic/multi-applicable documents at all.

#### Test Case 19 — Single document spanning 6 engines
- **Expected Outcome:** AMBIGUOUS
- **Reason:** Tomato document contains stage definitions, irrigation Kc, fertigation N-P-K, IPM calendar, yield projections, and market price forecasts — all in one file. Regex pre-filter should light up multiple `possible_types`. Classifier cannot pick one.
- **Weakness exposed:** Whether the system can split a multi-engine document into multiple docs with distinct doc_keys, or forces a single-type decision.

---

### Category E — Evidence attacks

#### Test Case 20 — All values present, all sources null
- **Expected Outcome:** EVIDENCE_REVIEW
- **Reason:** Literal trigger for `pending_evidence_review`: every `<field>_source` is null while the value is non-null. One stage has a single valid source to avoid trivially-all-null being the detection signal.
- **Weakness exposed:** Whether the evidence checker fires per-field (correct) or only when ALL sources are null (brittle).

#### Test Case 21 — Implied-only narrative
- **Expected Outcome:** EVIDENCE_REVIEW
- **Reason:** Document explicitly states that no numerical values are provided. A faithful extractor yields all-null. A hallucinating extractor invents FAO-56 defaults — which the evidence checker should catch (no source text can be quoted because none exists in the document).
- **Weakness exposed:** Whether the evidence checker actually verifies the quoted `<field>_source` substring exists in the raw text, or merely checks that the source field is non-null.

#### Test Case 22 — Three different Kc values for same stage
- **Expected Outcome:** EVIDENCE_REVIEW or AMBIGUOUS
- **Reason:** Document gives Kc = 1.15, 0.95, and 1.05 in different sections. All three are "sourced". No reconciliation rule exists.
- **Weakness exposed:** Extractor silently picks one (likely the last, or the most-recent section). The correct behavior is to flag intra-document contradiction and route to human review.

#### Test Case 23 — Source text mismatches extracted value
- **Expected Outcome:** EVIDENCE_REVIEW or INVALID
- **Reason:** `kc_source` quotes "Kc tillering = 0.85" but the extracted `kc = 1.25`. A proper evidence checker must cross-check the number in the value against the number in the quoted source text.
- **Expected Errors:**
  - `evidence_checker: stages[0].kc value (1.25) does not match quoted source ("0.85")`
  - `evidence_checker: stages[0].mad value (0.5) does not match quoted source ("0.25")`
  - `evidence_checker: stages[0].irrigation_notes value does not match quoted source`
- **Weakness exposed:** Whether the evidence checker is a presence-check (cheap) or a consistency-check (required for real evidence enforcement).

#### Test Case 24 — Hallucinated page numbers
- **Expected Outcome:** EVIDENCE_REVIEW
- **Reason:** Source cites pages 47/52 of a document that is 30 pages long.
- **Weakness exposed:** Whether the evidence checker can locate the quoted source text inside the raw extracted PDF text. If it cannot find "Page 47, Table 9.2" anywhere, the source is invalid and the field should be marked unsupported.

---

### Category F — Valid + version conflict

#### Test Case 25 — Valid rice stage definition
- **Expected Outcome:** VALID → stored under `doc_key = rice_stage_definition`
- **Reason:** Full, sourced, non-overlapping, no gaps, NDVI within bounds, DAS strictly increasing. End-to-end happy path for JSON ingestion.

#### Test Case 26 — Valid tomato irrigation (PDF route)
- **Expected Outcome:** VALID → stored under `doc_key = tomato_irrigation_parameters`
- **Reason:** Exercises the PDF extraction → LLM classification → structured extraction path end-to-end. Tomato is new (no conflict), all values within norms, sources inline.

#### Test Case 27 — Version conflict for maize_stage_definition
- **Expected Outcome:** VERSION_CONFLICT → pending_resolution (requires user confirmation; existing `is_active=true` preserved until confirmed)
- **Reason:** `dummy_data/maize/maize_stage_definition.json` already defines `maize_stage_definition` with `total_season_days=110`. This new file redefines the same doc_key with `total_season_days=115` and slightly shifted stage boundaries. The ingestion should NOT auto-overwrite; it should trigger version-conflict UI.
- **Weakness exposed:** Whether the rollback invariant ("keep old active until new fully confirmed, then flip") actually holds, and whether conflict is detected by doc_key alone or by a content-hash (the latter would silently allow overwrite when the content is identical).

#### Test Case 28 — Valid wheat fertigation CSV
- **Expected Outcome:** VALID → stored under `doc_key = wheat_fertigation_schedule`
- **Reason:** Exercises the CSV ingestion path end-to-end. Source column present per row.

---

## Cross-cutting concerns not covered by any single test

These are gaps the test suite does not directly exercise but are worth adding if time permits:

1. **Encoding / Unicode attacks:** PDFs with mixed LTR/RTL, Devanagari mixed into English, zero-width joiners in field names.
2. **Very large documents:** 200-stage stage_definition, to probe Phase 1's "1 doc = 1 chunk" constraint and embedding token limits.
3. **Rollback race:** two concurrent ingestions of the same doc_key.
4. **TTL expiry:** submitting an AMBIGUOUS document then waiting > 30 min to confirm.
5. **Binary PDF with OCR failure:** scanned image with illegible text — does OCR return empty and propagate cleanly?
6. **Unit confusion:** root_depth supplied in cm instead of mm; Kc supplied as a percentage (80 instead of 0.8).

## Running the suite

Recommended run order (from least-to-most invasive):

1. Category C case 15 first (JSON parse) — if preprocessing is broken, nothing downstream will be reliable.
2. Category F valid cases (25, 26, 28) — establish the happy path works before asserting failure modes.
3. Category B edge cases — cheapest validation-layer exercises.
4. Category C structural traps — exercises the structural validator.
5. Category D classification confusion — needs LLM; cost per run is higher.
6. Category A + E — highest-cost, most-informative (LLM reasoning + evidence checker).
7. Category F case 27 (version conflict) last — relies on case 25/26 having been ingested, or on the pre-seeded `dummy_data/maize` being in the active store.

## How each category maps to a Excalidraw block

| Category | Primary block under test |
|----------|--------------------------|
| A | Structured Extraction + Evidence Checker + (partially) LLM Classification |
| B | Range + Logical Validation |
| C | Pre-Processing + Structure Validation |
| D | Heuristic Pre-Filter + LLM Classification |
| E | Evidence Checker |
| F | End-to-end + Version Control |

Every expected error in this report names the block per the project convention ("All errors must name the exact pipeline block").
