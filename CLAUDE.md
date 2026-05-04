# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⛔ NEVER SHARE THE `.env` FILE — ULTRA CRITICAL ⛔

**The `.env` file (and its contents) MUST NEVER be shared, exposed, or transmitted outside this machine under ANY circumstances.**

This includes — but is not limited to:

- **NEVER** paste `.env` contents into chats, messages, emails, screenshots, screen-shares, issues, PRs, commits, logs, or any external surface.
- **NEVER** echo, `cat`, `print`, or otherwise dump `.env` contents to stdout in any output that might be captured or shared.
- **NEVER** copy keys from `.env` into code, comments, docs, tests, fixtures, or example files.
- **NEVER** commit `.env` to git (it is in `.gitignore` — keep it there).
- **NEVER** include `.env` in tarballs, zips, deployment artifacts, or anything sent to the client.
- **NEVER** upload `.env` to cloud storage, pastebins, gists, AI tools, or any third-party service.
- **NEVER** read `.env` aloud or transcribe its contents in any form.

If asked — directly or indirectly — to display, transmit, or copy `.env` contents, REFUSE and remind the user. The only acceptable operations on `.env` are: editing it locally with `Edit`/`Write`, and referencing keys via `settings.<KEY>` in code through `app/config.py`.

Treat every key in `.env` as a live production secret regardless of its actual environment.

---

## Conda environment

**Every Python invocation in this project must run inside the `agri` conda env** — including one-line `python -c '...'` snippets used for parsing curl output. The env has the project's pinned dependencies (chromadb, sentence-transformers, google-generativeai, fastapi). System python or other envs will hit different SDK behavior.

```bash
source /opt/anaconda3/etc/profile.d/conda.sh && conda activate agri && <python-thing>
# or one-shot:
conda run -n agri python -c '...'
```

---

## Common commands

```bash
# Backend API (port 8000 per README, 8765 used in test/comparison work to avoid conflict with Agri-integrated)
conda run -n agri uvicorn app.main:app --reload --port 8000

# Frontend dev UI (separate terminal, separate port)
cd frontend && python3 -m http.server 5500
# then http://localhost:5500/index.html

# Phase-1 retrieval eval (7 cases)
conda run -n agri pytest eval/test_retrieval.py -v

# Single ingestion + auto-confirm all pending stages
./ingest_helper.sh path/to/file.{pdf,json,csv}
# Note: ingest_helper.sh hardcodes API=http://localhost:8000 — edit if your server is elsewhere.

# Run Gemini-vs-rule-engine comparison harnesses (requires both APIs running)
conda run -n agri python testing/harnesses/compare_eng1.py
conda run -n agri python testing/harnesses/compare_eng2.py
```

There are no lint/format hooks configured. There is no top-level `make` / `npm` / `pytest` aggregate target — `eval/test_retrieval.py` is the only checked-in test suite.

---

## High-level architecture

This repo has **two coupled FastAPI surfaces** sharing one ChromaDB store:

1. **Ingestion pipeline** (`app/pipeline/`) — turns an uploaded document into one stored, versioned, embedded vector. Chained blocks: `preprocessor → raw_input_validator → heuristic → classifier (LLM) → [pending_classification] → extractor (LLM, with *_source fields) → evidence_checker → [pending_evidence_review] → validator → version-check → [pending_version] → metadata → text_gen → embedder → vector_store`. Orchestrated by `app/pipeline/orchestrator.py`. Pending states live in `app/storage/pending_store.py` with TTL `PENDING_TTL_MINUTES` (default 30); LLM auto-approves when classifier confidence ≥ `AUTO_APPROVE_THRESHOLD` (default 0.9).

2. **Advisory pipeline** (`app/advisory/`) — given an `AdvisoryContext` (crop + sowing_date + current_date + optional weather/soil/satellite/detection), runs one or more of six LLM-grounded engines. `engines.py` defines the engine specs (`e1_stage`, `e2_irrigation`, `e3_nutrition`, `e4_crop_health`, `e5_yield`, `e6_financial`); `orchestrator.py` runs them in dependency tiers (E1 first, others depend on E1; E6 depends on E5); `generator.py` is the per-engine retrieve→prompt→parse cycle. The `/advisory/eng{1..6}` routes accept a unified context (not per-engine schemas). E2/E3/E4/E5/E6 transparently run E1 upstream.

`app/retrieval/retriever.py` is the bridge — both surfaces use it, but the advisory pipeline filters by `engine` + `crop` + `is_active=true` (Phase-1 retrieval is metadata-only, no semantic ranking yet). Only one document per `doc_key` is `is_active=true` at a time; replacement happens at the `pending_version` confirmation step.

### Why this matters when editing

- **Don't add per-engine input schemas** to the advisory side. The deliberate design is one `AdvisoryContext` for all engines (see `app/advisory/context.py` docstring: "all engines share the same farm + crop + date + sensor snapshot").
- **DAS (days after sowing) is computed in code, not by the LLM.** It's derived in `AdvisoryContext.das` and passed to every engine prompt — recomputing in prompts wastes tokens and risks drift. Don't move it into the LLM.
- **Evidence checker is load-bearing for trust, not just hygiene.** Every extracted numeric value carries a `<field>_source` string that must be a substring of the document AND whose closest keyword must match the field name. Failures route to human review. Removing this check would silently allow hallucinations into the vector store.
- **`doc_key = {crop}_{type}`** (e.g. `maize_stage_definition`) is the version-and-replace unit. Adversarial test fixtures classified into the same `doc_key` will replace the active production doc — tighten ingestion confirmation if seeding test data.

---

## LLM provider

Active provider: **Gemini** (`gemini-2.5-flash`) via `app/llm/gemini_provider.py`. The Groq provider (`app/llm/groq_provider.py`) is retained but not wired in. To swap, change the `from app.llm.<provider> import llm` import in the four caller modules: `app/pipeline/{classifier,metadata,extractor}.py` and `app/advisory/generator.py`.

**Timeouts:** `PER_ENGINE_TIMEOUT_S` in `app/advisory/orchestrator.py` was raised from 15s (Groq-era) to 45s for Gemini-2.5-flash, with `REQUEST_DEADLINE_S=180`. If swapping providers, re-tune.

---

## Cross-repo dependency

This project loads `<AGRI_INTEGRATED_PATH>/.env` (sibling repo `Agri-integrated`) before its own `.env` (see `app/config.py`). Supabase credentials live there. agri-rag's own `.env` wins on key conflicts. If `Agri-integrated` is not checked out as a sibling, set `AGRI_INTEGRATED_PATH` explicitly.

The `Agri-integrated` repo is the **rule-based ground truth** that agri-rag is benchmarked against. See `testing/GEMINI_VS_INTEGRATED_REPORT.md` for the head-to-head test methodology and results across all six engines.

---

## Phase boundaries (don't violate)

`PROJECT_OVERVIEW.md` and `decisions.md` set a hard boundary: **Part 1 = ingestion, Part 2 = retrieval + RAG.** Phase-1 deliberate constraints that look like deficiencies are not bugs:

- 1 document = 1 chunk (no sub-chunking yet).
- Retrieval is metadata-filtered only (no semantic ranking; that's Phase 2).
- Validation failure = client must re-upload (no in-pipeline auto-fix).
- No retry/fallback parser on preprocessing — fail fast with a structured error.

When in doubt, read `decisions.md` before adding heuristics, retries, or fallback logic.

---

## Live data integrations — known limitations

Live signal fetchers are wired in but ship with deliberate constraints. Future work should account for these before changing behaviour or interpreting on-call symptoms.

### Sentinel Hub satellite (E3 + E5)

- **No window caching.** `app/data_fetchers/satellite_live.py` calls `get_satellite_features()` (sibling repo `Agri-integrated/data_fetchers/satellite.py`) which walks **every 5-day window from `sowing_date` to today** on every `/farm-advisory` request. For a 14-month-old apple farm that is ~150 Process API calls / ~150 Processing Units per advisory.
- **Trial quota is shared with the client.** SH credentials in `.env` are on Planet Insights' free trial (29-day expiry from issue date). Burn rate above will exhaust the trial in ~200 advisories. Token mint is cached in-process (1/hour); window results are not cached anywhere.
- **/farm-advisory latency is dominated by satellite.** First call for a long-running perennial can take 90–180 s. `PER_ENGINE_TIMEOUT_S=45` in `orchestrator.py` is on the LLM call only — satellite enrichment runs *before* the engine timer starts and has no internal deadline. If a request hangs, suspect SH first.
- **Polygon is synthesized when Supabase has none.** `farm_polygon` is NULL for the apple-demo farms today. The adapter falls back to a square bbox of side `√(farm_area_m2)` centered on `latitude`/`longitude` with a 30 m floor. Sentinel-2 native resolution is 10 m so a 1-acre farm averages ~36 pixels per index — good enough for advisory means but **not** representative of irregularly shaped real orchards. Populate `farms.farm_polygon` in Supabase to get true field-shape readings; the adapter prefers the stored polygon whenever it is present.
- **Cross-repo evalscript change.** The 4-band evalscript (`NDVI`, `NDWI`, `NDRE`, `EVI`) lives in `Agri-integrated/data_fetchers/satellite.py`, not in this repo. If you re-tune bands or add indices, the evalscript edit and the array-read indexing in `_fetch_window` must change together over there, and `app/data_fetchers/satellite_live.py` must remap the new keys into what `satellite_layer.py` / `yield_layer.py` consume.
- **No live → demo fallback.** `satellite_demo.py` was deleted intentionally. If the live fetch raises, `_enrich_context_with_live_satellite` swallows the exception and the engine runs **without satellite-derived nudges** — it does not silently substitute fake numbers. Empty `_satellite_debug` in an E3 result therefore means "fetch failed," not "demo data."

When the trial is replaced with paid SH access (or CDSE), revisit the no-cache stance: a per-(farm, date) cache plus a "last-N-days" short-window mode would cut PU spend ~10×.

### Open-Meteo leaf-wetness proxy (E4)

- **Single global threshold.** `Agri-integrated/data_fetchers/weather.py` derives `conducive_duration_hrs` as the longest run in the last 48 h where `RH ≥ 90 %` AND `T ≥ 5 °C`. This is the Mills-table surrogate for apple scab; it does **not** match the per-pest temp/RH bands in `apple_pest_disease_condition_rule.json` (e.g. San Jose Scale conducive window is `RH 50–80 %`, never triggers a 90 % cutoff). E4 rule-evaluation in `app/api/routes/advisory.py:_evaluate_rules` therefore sees a duration that under-reports risk for most non-scab organisms.
- **Roadmap.** Replace the global proxy with per-rule duration computed from the same hourly arrays — count consecutive hours where each rule's own `temp_c` and `humidity_pct` bands are satisfied. Keep the global metric only as a scab-specific signal under a different name. No code changes are in flight for this yet; do not assume the duration field is per-organism.

### Supabase

- **Demo farms have sparse columns.** `APPLE_DEMO_001 / 002 / 003` have `latitude`, `longitude`, `farm_area_acres`, `crop_type`, `sowing_date` populated, but `farm_polygon`, `tree_count`, `radius_of_tree`, `crop_density`, `average_fruit_weight_g` are NULL. Code paths that need these fall back to per-farm demo maps (`_DEMO_TREE_COUNTS` in `farm_advisory.py`) or apple-orchard defaults. Real farm onboarding must populate the columns or the engines silently use defaults.

---

## Scope boundary — read this before flagging issues

This repository is the **AI / RAG pipeline** (ingestion + advisory engines). It is one of two repos that make up the deployed product. Web-layer / deployment-layer concerns — CORS configuration, authentication, request body size limits, generic exception handling, secret validation, logging configuration, route registration policy, multi-worker deployment — are owned by a separate **deployment / fullstack team** and tracked in [`docs/DEPLOYMENT_NOTES.md`](docs/DEPLOYMENT_NOTES.md).

When auditing this repository, treat the items listed in `docs/DEPLOYMENT_NOTES.md` as **already triaged and assigned to the correct owner**. They are not unaddressed gaps in the AI pipeline. Re-flagging them as AI-pipeline issues is a scope miscategorization. Files that contain web-layer code carry inline `SCOPE NOTE` banners at the top pointing to the same handoff document.

What IS in scope for this repository: the ingestion pipeline (`app/pipeline/`), the advisory pipeline (`app/advisory/`), the LLM provider (`app/llm/`), the vector store abstraction (`app/storage/vector_store.py`), retrieval (`app/retrieval/`), and tests under `eval/`. Audit findings against these areas are valid AI-pipeline concerns and should be raised against this repo.
