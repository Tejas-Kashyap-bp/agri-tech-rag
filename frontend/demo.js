// Shared bootstrapper for the controlled-demo pages.
// Wires the "Generate advisory" button to POST the dropdown values and
// renders {summary, raw} into #output. Dropdown changes do NOT fire the
// request — the user must click the button explicitly.
//
// Pages pass `fields` as { request_key: select_element_id }. Values are
// pulled verbatim from the dropdowns — no free input is ever read.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

let _seq = 0;

async function initDemo({ endpoint, fields }) {
  const out = $("output");
  const btn = $("generate");

  async function run() {
    const my = ++_seq;
    const body = {};
    for (const [key, id] of Object.entries(fields)) {
      body[key] = $(id).value;
    }
    btn.disabled = true;
    out.innerHTML = `<span class="muted"><span class="spinner"></span>Running engine…</span>`;
    let resp, data;
    try {
      resp = await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      data = await resp.json();
    } catch (e) {
      if (my === _seq) out.innerHTML = `<span class="err">Network error: ${esc(e.message)}</span>`;
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
    if (my !== _seq) return;  // stale response — newer request in flight
    if (!resp.ok) {
      out.innerHTML = `<span class="err">Error: ${esc(data.detail || data.message || resp.status)}</span>`;
      return;
    }
    render(data);
  }

  function render(data) {
    const o = data.output || {};
    const summary = o.summary || "(no summary)";
    // Static "why" block — derived from the same dropdown values, NOT from
    // the LLM response, so it cannot drift in wording. See demo_explain.js.
    const explainer = (window.DEMO_EXPLAINERS || {})[endpoint];
    // Pass both `inputs` (typed dropdown values) and `output` (engine
    // response). Most explainers only need inputs; pest-risk also needs
    // the deterministic scab numbers from output.details.apple_scab_final
    // so it can render LAI / LWD / wetness in the "Why" panel.
    const explainHtml = explainer ? explainer(data.inputs || {}, data.output || {}) : "";
    const scabHtml = renderAppleScab(o);
    out.innerHTML = `
      <h2>Output</h2>
      <p class="summary">${esc(summary)}</p>
      ${scabHtml}
      ${explainHtml ? `
        <details class="explain">
          <summary>▸ Why this output?</summary>
          ${explainHtml}
        </details>` : ""}
      <span class="toggle" onclick="this.nextElementSibling.classList.toggle('show')">▸ show raw output</span>
      <pre class="raw">${esc(JSON.stringify(data, null, 2))}</pre>
    `;
  }

  // The farmer-facing apple-scab line is already in the main summary
  // (see _farmer_friendly_scab_summary in app/api/routes/demo.py).
  // Technical detail (LAI / LWD / wetness / canopy / confidence) lives in
  // the "Why this output?" panel built by demo_explain.js. We intentionally
  // do NOT render a separate scary "always-on" card here — that was making
  // the output look alarming for farmers.
  function renderAppleScab(_o) { return ""; }

  btn.addEventListener("click", run);
}
