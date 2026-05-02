# Apple stage logic — reference for downstream data (pest, disease, fertigation)

This note captures the reasoning behind `apple_stage_definition.json` so the same conventions can be reused when building pest, disease, and fertigation documents.

## 1. Apple is perennial — DAS does not apply

The existing pipeline schema requires `start_das` / `end_das` integers and validates continuity (no gaps, no overlaps) over `[0, 365]`. Apple stages are calendar-based, not days-after-sowing.

**Convention adopted:** treat `start_das` / `end_das` as **day-of-perennial-cycle**, with **March 1 = day 1** and end of February = day 365. This:
- Keeps the existing validator unchanged (single contiguous block, sorted, no wrap).
- Aligns "day 1" with the start of the active growing season (vegetative).
- Pushes dormancy (Dec–Feb) to the end of the cycle as a single contiguous range.

The runtime advisory layer (`AdvisoryContext.das`) will need a perennial-aware computation in Phase 2; for now this encoding satisfies ingestion + validation cleanly.

## 2. Source-of-truth for stage windows

`Master data Apple Insect pest.xlsx` — column **"Crop Age (Days)"**. Verified consistent: each stage maps to exactly one date range across all 10 pests × 4 stages (40 rows). No client clarification needed on consistency.

| Stage | Source date range | Day-of-cycle |
|---|---|---|
| Vegetative | March – 10 April | 1 – 41 |
| Flowering | 11 April – 10 May | 42 – 71 |
| Fruiting | 11 May – 15 Aug | 72 – 168 |
| Harvesting | 16 Aug – Nov | 169 – 275 |
| Dormancy *(added)* | Dec – end of Feb | 276 – 365 |

## 3. The dormancy gap

The source sheet covers March–November only. December–February is real, named, and agronomically meaningful: the tree is leafless, no active growth, and this is the window for **pruning, dormant sprays, and chilling-hour accumulation**.

**Decision:** name it `Dormancy` (standard horticultural term), not `stage_x`. Pest/disease/fertigation engines for this window should return dormancy-appropriate guidance (pruning, dormant spray reminders, no fertigation) rather than fabricating active-season recommendations.

## 4. Open question for client (non-blocking)

`16 Aug-Nov` boundary — does harvesting end **15 Nov** (mirroring `16 Aug`) or **30 Nov**? Currently encoded as **30 Nov** so dormancy starts **Dec 1**. Confirm and adjust if needed; only the `S4.end_das` and `S5.start_das` integers shift.

## 5. Reuse for pest & disease data

When building pest and disease documents from the same Excel sources:
- Reuse the **same five stage codes** (`S1` Vegetative … `S5` Dormancy) so retrieval can join pest/disease rows to stage windows by `stage_code`.
- Source rows that span "March–10 April" etc. map cleanly to `S1`–`S4`. Rows have **no entry for dormancy** — that is expected; do not invent dormancy-period pest/disease pressure values.
- For dormancy, document any pruning-time / dormant-spray practices separately if the client provides them; otherwise the engine response for `S5` is "no active pest pressure during dormancy; pruning and dormant spray window."
