// Static explanation tables for each demo engine.
//
// Each engine exports a function: explain<Engine>(inputs) → HTML string.
// The function reads the (already-typed) dropdown values and returns the
// markup for the "Why this output?" panel. NO API call is made — every
// number/condition shown here is derived from the same constants the
// backend uses, so the explanation cannot drift from the request payload.
//
// WHY hardcoded (not derived from the LLM response):
//   The dropdowns are intentionally a finite, frozen vocabulary, so every
//   reachable input combination has one deterministic explanation. Pulling
//   it from the LLM output would invite wording drift and require a
//   classifier just to detect "same vs different." Static lookup is exact,
//   instant, and has no surprise wording.

const cell = (k, v) => `<tr><td class="k">${k}</td><td class="v">${v}</td></tr>`;
const block = (title, rows, note) => `
  <div class="explain-block">
    <h4>${title}</h4>
    <table class="explain-table">${rows.join("")}</table>
    ${note ? `<p class="explain-note">${note}</p>` : ""}
  </div>`;

// ── stage windows shared by Crop Stage + downstream engines ────────────────
// Calendar windows from apple_stage_definition (HP/J&K, ~5,000 ft baseline).
// Altitude pushes the windows LATER by 10–15 days per +1,000 ft.
const STAGE_WINDOWS_BASELINE = [
  { stage: "Dormant",            window: "Dec 1 – Feb 28" },
  { stage: "Bud Break",          window: "Mar 1 – Mar 31" },
  { stage: "Flowering",          window: "Apr 1 – May 31" },
  { stage: "Fruit Development",  window: "Jun 1 – Aug 15" },
  { stage: "Maturity / Harvest", window: "Aug 16 – Oct 31" },
  { stage: "Post-Harvest",       window: "Nov 1 – Nov 30" },
];

const ALT_DELAY_DAYS = { "1000 ft": -50, "3000 ft": -20, "6000 ft": +10 };

// ── 1. crop stage ───────────────────────────────────────────────────────────
function explainCropStage({ month, altitude }) {
  const delay = ALT_DELAY_DAYS[altitude] ?? 0;
  const sign = delay > 0 ? `+${delay}` : `${delay}`;
  const altNote = delay === 0
    ? "Altitude matches the 5,000 ft baseline — no shift."
    : `Altitude shifts the calendar by <b>${sign} days</b> from the 5,000 ft baseline (≈ 10–15 days per 1,000 ft).`;

  const rows = STAGE_WINDOWS_BASELINE.map(s =>
    cell(s.stage, s.window)
  );

  const bucketToStage = {
    "Dec–Feb (Dormant)":     "Dormant",
    "March (Bud Break)":     "Bud Break",
    "Apr–May (Flowering)":   "Flowering",
    "Jun–Aug (Fruit Dev.)":  "Fruit Development",
    "Sep–Nov (Maturity)":    "Maturity / Harvest",
  };
  const expected = bucketToStage[month] || "—";

  return block(
    "Stage logic",
    rows,
    `<b>Selected window:</b> ${month} → expected baseline stage: <b>${expected}</b>.<br>${altNote}`
  );
}

// ── 2. fertilizer ───────────────────────────────────────────────────────────
const FERT_SCHEDULE = {
  "Dormant":           "Pre-bloom basal: full P + K + 1/3 N",
  "Bud Break":         "Foliar boron + 1/3 N split",
  "Flowering":         "Hold N — bloom is sensitive to lush growth",
  "Fruit Set":         "1/3 N split + Ca foliar (bitter-pit prevention)",
  "Fruit Development": "Final 1/3 N + K split for fruit sizing",
  "Maturity":          "No fertigation — taper off pre-harvest",
  "Post-Harvest":      "Light P + K for root reserves",
};
const SOIL_NUDGE = {
  "Low SOC":  "↑ +25% N (poor mineralisation, SOC 0.3%)",
  "Normal":   "no change (SOC ~0.9%, pH 6.2)",
  "High SOC": "↓ −15% N (rich soil, SOC 1.8%)",
};
const FIELD_NUDGE = {
  "Healthy":          "no field-condition adjustment (NDVI ~0.78)",
  "Moderate Stress":  "reduce N split, add foliar feed (NDVI ~0.55, ↓7d)",
  "Severe Stress":    "skip this fertigation, diagnose stress first (NDVI ~0.32, ↓7d)",
};

function explainFertilizer({ crop_stage, soil_health, field_condition }) {
  const rows = [
    cell("Stage schedule",  FERT_SCHEDULE[crop_stage] || "—"),
    cell("Soil adjustment", SOIL_NUDGE[soil_health]   || "—"),
    cell("Field condition", FIELD_NUDGE[field_condition] || "—"),
  ];
  const note = field_condition === "Severe Stress"
    ? "Severe-stress guardrail overrides the schedule — fertilizer pushed back until cause is diagnosed."
    : "Final recommendation = stage schedule × soil multiplier × field-condition guardrail.";
  return block("Fertilizer logic", rows, note);
}

// ── 3. pest risk ────────────────────────────────────────────────────────────
// Base rules from apple_pest_disease_condition_rule (subset shown).
const PEST_RULES = [
  { org: "Apple Scab",      tBand: "16–24°C", rhBand: "≥ 90%",  durHrs: "9–16 h",  tLo: 16, tHi: 24, rhLo: 90, rhHi: 100, dLo: 9,  dHi: 16 },
  { org: "Powdery Mildew",  tBand: "10–25°C", rhBand: "70–90%", durHrs: "6–24 h",  tLo: 10, tHi: 25, rhLo: 70, rhHi: 90,  dLo: 6,  dHi: 24 },
  { org: "Codling Moth",    tBand: "≥ 16°C",  rhBand: "any",    durHrs: "≥ 12 h",  tLo: 16, tHi: null, rhLo: 0,  rhHi: 100, dLo: 12, dHi: null },
  { org: "San Jose Scale",  tBand: "20–30°C", rhBand: "50–80%", durHrs: "any",     tLo: 20, tHi: 30, rhLo: 50, rhHi: 80,  dLo: 0,  dHi: null },
];

function _inBand(v, lo, hi) { return v >= lo && (hi == null || v <= hi); }
function _evalPestTriggers(t, rh, dur) {
  return PEST_RULES
    .filter(r => _inBand(t, r.tLo, r.tHi) && _inBand(rh, r.rhLo, r.rhHi) && _inBand(dur, r.dLo, r.dHi))
    .map(r => r.org);
}
function explainPestRisk({ crop_stage, temperature, humidity, duration }, output) {
  const t = { "Cool (15°C)": 15, "Mild (22°C)": 22, "Warm (28°C)": 28 }[temperature];
  const rh = { "Dry (40%)": 40, "Moderate (70%)": 70, "Humid (95%)": 95 }[humidity];
  const dur = { "Short (8 h)": 8, "Medium (12 h)": 12, "Long (24 h)": 24, "Very Long (48 h)": 48 }[duration] ?? 0;
  const triggered = _evalPestTriggers(t, rh, dur);

  const ruleRows = PEST_RULES.map(r =>
    cell(r.org, `temp ${r.tBand} · RH ${r.rhBand} · duration ${r.durHrs}`)
  );
  const inputRow = cell("Snapshot", `temp <b>${t}°C</b> · RH <b>${rh}%</b> · conducive duration <b>${dur} h</b>`);
  const triggerLine = triggered.length
    ? `Triggered: <b>${triggered.join(", ")}</b>`
    : "No rule satisfied — preventive cover only.";
  const ruleBlock = block(
    "Pest-rule evaluation",
    [inputRow, ...ruleRows],
    `Stage <b>${crop_stage}</b>. ${triggerLine}`
  );

  // ── Apple Scab technical detail ───────────────────────────────────────
  // Moved here from the main output panel. Technical labels (LWD / ASRI /
  // LAI / canopy density / confidence) belong in the "Why this output?"
  // drawer so the farmer-facing summary stays plain-language.
  const scabBlock = _scabExplainBlock(output);
  return ruleBlock + scabBlock;
}

function _scabExplainBlock(output) {
  const det = (output && output.details) || {};
  const fin = det.apple_scab_final;
  if (!fin) return "";
  const lai = det.lai_biomass_scab_guardrail || {};
  const lwd = (fin.lwd_hours == null) ? "—" : `${fin.lwd_hours} h`;
  const laiVal = (fin.lai_value == null || fin.lai_value === "UNKNOWN")
    ? "—" : fin.lai_value;
  const rows = [
    cell("Risk (after canopy adj.)", `<b>${fin.adjusted_risk || "UNKNOWN"}</b>`),
    cell("Base risk (weather only)", fin.base_risk || "UNKNOWN"),
    cell("Leaf wetness", `${fin.wetness_status || "UNKNOWN"} · LWD ${lwd}`),
    cell("Canopy density (LAI)", `${fin.canopy_density || "UNKNOWN"} (LAI ${laiVal})`),
    cell("Canopy effect on risk", fin.lai_effect || "UNKNOWN"),
    cell("Confidence", `${fin.final_confidence == null ? "—" : fin.final_confidence}/100`),
  ];
  const note = lai.reason
    ? `<i>${lai.reason}</i> ASRI / LWI / LWD are computed deterministically from the weather snapshot; LAI comes from NDVI via the Beer–Lambert proxy. None of these come from the LLM.`
    : "ASRI / LWI / LWD are computed deterministically from the weather snapshot; LAI comes from NDVI via the Beer–Lambert proxy. None of these come from the LLM.";
  return block("Apple Scab — technical detail", rows, note);
}

// ── 4. IPM ──────────────────────────────────────────────────────────────────
const IPM_BLOCK_BY_STAGE = {
  "Dormant":           { window: "Nov 15 – Feb 28", lead: "Dormant oil + copper" },
  "Bud Break":         { window: "Mar 1 – Mar 31",  lead: "Pre-bloom Mancozeb (scab cover)" },
  "Flowering":         { window: "Apr 1 – May 31",  lead: "Avoid insecticide during bloom; rotate fungicide" },
  "Fruit Set":         { window: "Jun 1 – Jun 30",  lead: "Calcium + first codling-moth spray" },
  "Fruit Development": { window: "Jul 1 – Aug 15",  lead: "Codling-moth + scab cover sprays" },
  "Maturity":          { window: "Aug 16 – Oct 15", lead: "PHI-aware sprays only" },
  "Post-Harvest":      { window: "Oct 16 – Nov 14", lead: "Sanitation + leaf-litter management" },
};
const SPRAY_VOL_PER_TREE = 10; // litres per tree (default scaling basis)
const ORG_DOSE = {
  "Apple Scab":      { mat: "Mancozeb 75% WP", per100L: "200 g", basis: "spray_solution" },
  "Codling Moth":    { mat: "Spinosad 45% SC", per100L: "20 mL", basis: "spray_solution" },
  "San Jose Scale":  { mat: "Horticultural mineral oil", perTree: "50 mL", basis: "per_tree" },
  "Powdery Mildew":  { mat: "Sulphur 80% WG", per100L: "300 g", basis: "spray_solution" },
  "None":            null,
};

function explainIpm({ crop_stage, tree_count, triggered_organism }) {
  const trees = parseInt(tree_count, 10);
  const totalSpray = trees * SPRAY_VOL_PER_TREE;
  const blk = IPM_BLOCK_BY_STAGE[crop_stage] || {};
  const dose = ORG_DOSE[triggered_organism];

  const rows = [
    cell("Stage window", blk.window || "—"),
    cell("Spray volume", `${trees} trees × ${SPRAY_VOL_PER_TREE} L/tree = <b>${totalSpray} L</b>`),
    cell("Lead action",  blk.lead || "—"),
  ];

  let calc = "";
  if (!dose) {
    calc = "No specific organism triggered — preventive block only.";
  } else if (dose.basis === "spray_solution") {
    const num = parseFloat(dose.per100L);
    const unit = dose.per100L.replace(/[\d.\s]/g, "");
    const qty = (num * totalSpray / 100).toFixed(1);
    rows.push(cell("Material", dose.mat));
    rows.push(cell("Dose math", `${dose.per100L} per 100 L × ${totalSpray} L ÷ 100 = <b>${qty} ${unit}</b>`));
    calc = `Mix <b>${qty} ${unit}</b> of ${dose.mat} in <b>${totalSpray} L</b> water.`;
  } else if (dose.basis === "per_tree") {
    const num = parseFloat(dose.perTree);
    const unit = dose.perTree.replace(/[\d.\s]/g, "");
    const total = (num * trees).toFixed(0);
    rows.push(cell("Material", dose.mat));
    rows.push(cell("Dose math", `${dose.perTree} per tree × ${trees} trees = <b>${total} ${unit}</b>`));
    calc = `Apply <b>${total} ${unit}</b> of ${dose.mat} across the orchard.`;
  }
  return block("IPM logic", rows, calc);
}

// ── 5. yield ────────────────────────────────────────────────────────────────
const YIELD_ADJ = {
  "Healthy":         { pct: +10, why: "NDVI ~0.78 (healthy), 7-day trend +0.03 → +10%" },
  "Moderate Stress": { pct: -5,  why: "NDVI ~0.55 (mid), 7-day trend −0.02 → −5%" },
  "Severe Stress":   { pct: -25, why: "NDVI ~0.32 (low), 7-day trend −0.08 → −25%" },
};
const RADIUS_M = 0.10, DENSITY = 2000, FRUIT_W_G = 150;

function explainYield({ crop_stage, tree_count, field_condition }) {
  const trees = parseInt(tree_count, 10);
  const tcsa = Math.PI * RADIUS_M * RADIUS_M;
  const basePerTree = +(tcsa * DENSITY * FRUIT_W_G / 1000).toFixed(2);  // kg
  const adj = YIELD_ADJ[field_condition];
  const finalPerTree = +(basePerTree * (1 + adj.pct / 100)).toFixed(2);
  const total = +(finalPerTree * trees).toFixed(2);

  const rows = [
    cell("TCSA (trunk area)",
      `π·r² = π × ${RADIUS_M}² = <b>${tcsa.toFixed(4)} m²</b>`),
    cell("Base per tree",
      `TCSA × density × fruit_weight = ${tcsa.toFixed(4)} × ${DENSITY} × ${FRUIT_W_G} g = <b>${basePerTree} kg</b>`),
    cell("Field adjustment", `${adj.pct > 0 ? "+" : ""}${adj.pct}% — ${adj.why}`),
    cell("Final per tree",
      `${basePerTree} kg × (1 ${adj.pct >= 0 ? "+" : "−"} ${Math.abs(adj.pct)}/100) = <b>${finalPerTree} kg</b>`),
    cell("Orchard total",
      `${finalPerTree} kg × ${trees} trees = <b>${total} kg</b>`),
  ];
  return block(
    "Yield calculation",
    rows,
    `Stage <b>${crop_stage}</b>. The geometry inputs (radius 0.10 m, density 2000, fruit weight 150 g) are apple-orchard defaults.`
  );
}

// ── registry ────────────────────────────────────────────────────────────────
window.DEMO_EXPLAINERS = {
  "/engine/crop-stage":  explainCropStage,
  "/engine/fertilizer":  explainFertilizer,
  "/engine/pest-risk":   explainPestRisk,
  "/engine/ipm":         explainIpm,
  "/engine/yield":       explainYield,
};
