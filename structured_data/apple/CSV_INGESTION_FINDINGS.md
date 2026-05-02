# Why the raw CSVs from `agri-apple-data/` fail ingestion

Audited: `Master data Apple Diseases.csv`, `Master data Apple Insect pest.csv`.

## Failure stage

`Structured Extraction` — the LLM extractor returns invalid JSON twice and the pipeline fails fast (per CLAUDE.md: "no retry/fallback parser on preprocessing — fail fast"). Sample error:

```
LLM failed to produce valid structured JSON after 2 attempts ...
"disease_name": "Apple Scab", "disease_name_source": "Apple Scab",
"max_humidity_pct": 100, ... "wetness_duration_hours": "9--12"
```

The extractor is honest — it gets garbage in, refuses to fabricate.

## Root cause — CSV shape vs. preprocessor assumption

`app/pipeline/preprocessor.py:_parse_csv` assumes **a single header row at index 0**. The supplied CSVs have a **3-row banner+merged-subheader structure**:

```
Row 0:  ,,,,,,Crop,Apple,,,,,,            ← banner row (mostly empty, "Crop"/"Apple" jammed in cols 6-7)
Row 1:  Insect/Pest name,Crop Age (Days),Stage,Temp,,Humidity %,,Duration (hrs),Risk %,,,,,
Row 2:  ,,,Min,Max,Min,Max,,,,,,,        ← Min/Max sub-row for Temp + Humidity
Row 3:  San Jose Scale,March-10 April,vegetative,10,20,50,80,6--8,50,...
```

The preprocessor reads row 0 as the column header, so the parsed records look like
`{"": "San Jose Scale", "Crop": "March-10 April", "Apple": "vegetative", ...}` — total nonsense. The classifier may still tag it correctly (the cell *contents* contain "vegetative", "flowering", "Apple", etc.), but the extractor downstream cannot map mangled keys to schema fields.

## Other shortcomings observed

| # | Issue | Effect |
|---|---|---|
| 1 | Banner row + merged sub-header (above) | Fatal — parsed records have wrong keys |
| 2 | UTF-8 BOM (`﻿`) at file start of pest CSV | First parsed key becomes `"﻿"` (silent, harms classification) |
| 3 | ~5 trailing empty columns per row | Token bloat, LLM has to skip nulls |
| 4 | ~20 trailing empty rows at file end | Already filtered by the blank-row check, harmless but wasteful |
| 5 | Trailing whitespace in `flowering ` cell value | Stage-mapping mismatch unless `.strip()` |
| 6 | Range cells written as `9--12` (double-dash, string) | Extractor must parse a string range; not a numeric-coercion failure but adds an LLM step |

## What does work today

- The **two `.xlsx` source files** parse cleanly via `pandas.read_excel(..., header=None)` plus a known offset, because we control the offset. We already used this to build `apple_pest_disease_condition_rule.json` (one combined doc, 68 rules, ingested successfully).
- The **JSON file** built from those Excel sheets ingests in one pass — first try, no pending states.

## Empirical results — both CSVs fail

| File | Outcome |
|---|---|
| `agri-apple-data/Master data Apple Diseases.csv` (raw) | ❌ extractor — invalid JSON x2 |
| `structured_data/apple/apple_disease_condition_rule.csv` (cleaned: single header, no banner, no Min/Max sub-row, no trailing empties, range cells split into `*_min`/`*_max`) | ❌ extractor — invalid JSON x2 |
| `structured_data/apple/apple_pest_condition_rule.csv` (cleaned, same shape) | ❌ extractor — invalid JSON x2 |
| `structured_data/apple/apple_pest_disease_condition_rule.json` (canonical) | ✅ stored v1, no pending states |

**Cleaning the CSV shape is necessary but not sufficient.** Even with a perfect single-row header, the structured-extraction LLM step still rejects CSV-derived input because the `*_source` evidence fields it generates wrap the value in JSON-formatted quotes (`"\"pest_name\": \"San Jose Scale\""`), and the evidence checker rejects them — for CSV input, "the document text" is the JSON re-rendering of the parsed records, which the LLM quotes back to itself recursively.

## Fix paths (ranked)

1. **Best — ship the canonical JSON.** Done. `apple_pest_disease_condition_rule.json` is the active source of truth and is what retrieval reads. The CSVs are just a human-friendly export format for the same data.
2. **CSV-as-input parity (pipeline change, deferred):** the extractor needs a CSV-aware path that either skips evidence-checking on CSV (cells ARE the source — no extraction needed) or re-renders source text as flat CSV cell values rather than JSON. Out of scope for today.
3. **Pipeline-side fix for header rows (future):** extend `_parse_csv` with optional `skip_rows` / `header_rows=2` semantics. Per CLAUDE.md "no retry/fallback parser on preprocessing — fail fast" — this would be a deliberate Phase-2 enhancement, not a hotfix.

## Multi-doc retrieval / top_k

Doc-key uniqueness rule: `{crop}_{doc_type}` → only **one active doc** per (crop, doc_type). So if pest and disease are both `pest_disease_condition_rule`, you can have only one — the second ingest replaces the first via the version-and-replace step.

For E4 (`e4_pest_disease_risk`), the engine slot accepts **two distinct doc_types**:
- `pest_disease_condition_rule` (rule-based; current — pest+disease combined)
- `ipm_schedule` (calendar; future — when E4.2 IPM data arrives)

Default `k` raised **1 → 3** in `app/api/routes/farm_advisory.py` and `app/api/routes/advisory.py`. `k` is an upper bound; single-doc engines (E1/E3/E5/E6) just return their one doc, but E4 will pull `pest_disease_condition_rule` AND `ipm_schedule` together once both are in the store.

If you really want pest and disease as **separate** documents (not combined), the doc_type model needs one of:
- Use `crop_knowledge` doc_type (allows multiple via `{crop}_crop_knowledge_<slug>`).
- Add a slug suffix to `pest_disease_condition_rule` (small schema change).

Combining them in one JSON (current approach) is simpler, retrieves cleanly, and matches how the engine reasons over the rules anyway.
