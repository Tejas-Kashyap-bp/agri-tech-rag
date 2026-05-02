import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  Bug,
  ChevronDown,
  ClipboardList,
  Droplets,
  Eye,
  FlaskConical,
  Info,
  Leaf,
  Loader2,
  ShieldCheck,
  Sprout,
  Wheat,
} from "lucide-react";
import { demoFallback } from "./data/demoFallback";
import "./styles.css";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const DEFAULT_DEMO_FARM = "APPLE_DEMO_003";

// Hardcoded demo farm list — the apple build does not (yet) expose a /farms
// catalogue endpoint, so we surface a small list so the sidebar dropdown is
// not empty. APPLE_DEMO_003 is the row in Supabase the team is running the
// demo against.
const FARMS = [
  { id: "APPLE_DEMO_003", crop: "Apple", location: "Shimla, Himachal Pradesh", note: "Drip irrigation", farmer: "Demo Orchard" },
  { id: "APPLE_DEMO_001", crop: "Apple", location: "Kullu, Himachal Pradesh", note: "Drip irrigation", farmer: "Demo Orchard 1" },
  { id: "APPLE_DEMO_002", crop: "Apple", location: "Kinnaur, Himachal Pradesh", note: "Sprinkler", farmer: "Demo Orchard 2" },
];

// Engine card definitions. Order here drives render order.
// `subtitle` is the smart hint: it tells the farmer what kind of inputs drive
// each card without dumping the inputs themselves on the main view.
const ENGINE_CARDS = [
  {
    key: "stage",
    title: "Crop Stage",
    subtitle: "Where the orchard is in its yearly cycle, computed from the calendar date.",
    icon: Sprout,
    accent: "green",
  },
  {
    key: "irrigation",
    title: "Irrigation",
    subtitle: "Daily irrigation guidance.",
    icon: Droplets,
    accent: "blue",
    notApplicableWhenMissing: true,
    notApplicableMessage: "Not applicable for perennial apple — irrigation is managed at the orchard level, not as a daily decision.",
  },
  {
    key: "fertilizer",
    title: "Fertilizer (INM)",
    subtitle: "Next scheduled split, adjusted for soil and stage.",
    icon: FlaskConical,
    accent: "amber",
  },
  {
    key: "pest_disease_risk",
    title: "Pest & Disease Risk — Weather Forecast",
    subtitle: "Driven by today's temperature, humidity, and leaf-wetness duration. Tells you which organisms are likely to break out under the live conditions.",
    icon: Bug,
    accent: "red",
  },
  {
    key: "pest_disease_cure",
    title: "Pest & Disease — Spray Schedule",
    subtitle: "Itemised IPM plan: what to mix, how much per tree, total volume — scaled to your orchard size.",
    icon: ShieldCheck,
    accent: "red",
  },
  {
    key: "yield",
    title: "Yield & Harvest",
    subtitle: "Expected yield outlook and harvest window.",
    icon: Wheat,
    accent: "gold",
  },
];

function App() {
  const [farmId, setFarmId] = useState(DEFAULT_DEMO_FARM);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const selectedFarm = FARMS.find((f) => f.id === farmId);

  async function loadDemo() {
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const res = await fetch(`${API_BASE_URL}/ui-advisory/demo/${encodeURIComponent(farmId)}`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      setResult(data);
    } catch (err) {
      // Fall through to bundled fallback if the backend isn't reachable, so
      // the UI is still demoable offline. The drawer makes the source visible.
      setError(`Backend unreachable (${err.message}). Showing bundled fallback.`);
      setResult(demoFallback);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <Leaf size={28} strokeWidth={2.2} />
          <div>
            <strong>BluParrot Agri</strong>
            <span>Apple Advisory Console</span>
          </div>
        </div>

        <div className="controls">
          <label>
            <span>Farm</span>
            <select value={farmId} onChange={(e) => setFarmId(e.target.value)}>
              {FARMS.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.id} — {f.crop}
                </option>
              ))}
            </select>
          </label>

          <ProfileItem label="Crop" value={selectedFarm?.crop} />
          <ProfileItem label="Location" value={selectedFarm?.location} />
          <ProfileItem label="Irrigation" value={selectedFarm?.note} />
          <ProfileItem label="Farmer" value={selectedFarm?.farmer} />

          <button type="button" onClick={loadDemo} disabled={loading}>
            {loading ? <Loader2 className="spin" size={18} /> : <Eye size={18} />}
            {loading ? "Loading…" : "Load Demo"}
          </button>

          {result?.resolved_context ? (
            <LoadedValuesDrawer ctx={result.resolved_context} farm={result.farm} />
          ) : null}
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Apple advisory</p>
            <h1>Pick a farm and load the demo to run all engines.</h1>
          </div>
        </header>

        {error ? (
          <section className="message error">
            <AlertTriangle size={20} />
            <span>{error}</span>
          </section>
        ) : null}

        {!result && !loading ? <EmptyState /> : null}
        {loading ? <LoadingState /> : null}
        {result ? <AdvisoryResult result={result} /> : null}
      </main>
    </div>
  );
}

function ProfileItem({ label, value }) {
  return (
    <div className="profile-item">
      <span>{label}</span>
      <b>{value || "—"}</b>
    </div>
  );
}

// Loaded Values drawer: collapsed by default, opens to show every input value
// the engines actually saw (current_date, DAS, weather temp/humidity/leaf
// wetness, soil, satellite, tree_count). User asked for this NOT in the main
// UI but accessible from where Load Demo lives — this is exactly that spot.
function LoadedValuesDrawer({ ctx, farm }) {
  if (!ctx) return null;
  const safeExtra = ctx.extra && typeof ctx.extra === "object" ? ctx.extra : {};
  const safeWeather = ctx.weather && typeof ctx.weather === "object" ? ctx.weather : null;
  const safeSoil = ctx.soil && typeof ctx.soil === "object" ? ctx.soil : null;
  return (
    <details className="loaded-values">
      <summary>
        <Info size={15} />
        <span>Loaded values & reasons</span>
        <ChevronDown className="chevron" size={16} />
      </summary>
      <div className="loaded-values-body">
        <div className="loaded-section">
          <b>Source</b>
          <span className={`pill pill-source pill-${ctx.source}`}>{ctx.source}</span>
        </div>
        <KvList
          rows={[
            ["Crop", ctx.crop],
            ["Sowing date", ctx.sowing_date],
            ["Current date", ctx.current_date],
            ["Days after sowing", ctx.days_after_sowing],
            ...(farm
              ? [
                  ["Farm ID", farm.farm_id],
                  ["Farm name", farm.farm_name],
                  ["Location", `${farm.location?.district || "?"}, ${farm.location?.state || "?"}`],
                  ["Farm area (acres)", farm.farm_area_acres],
                  ["Tree count", farm.tree_count],
                  ["Irrigation", farm.irrigation_method],
                ]
              : []),
          ]}
        />
        {safeWeather ? (
          <>
            <div className="loaded-heading">Weather</div>
            <KvList rows={Object.entries(safeWeather)} />
          </>
        ) : null}
        {safeSoil ? (
          <>
            <div className="loaded-heading">Soil</div>
            <KvList rows={Object.entries(safeSoil)} />
          </>
        ) : null}
        <div className="loaded-heading">Extra</div>
        <KvList rows={Object.entries(safeExtra).filter(([, v]) => typeof v !== "object" || v === null)} />
      </div>
    </details>
  );
}

function KvList({ rows }) {
  const safeRows = Array.isArray(rows) ? rows : [];
  return (
    <dl className="kv-list">
      {safeRows
        .filter(([, v]) => v !== undefined && v !== null && v !== "")
        .map(([k, v], i) => (
          <div className="kv-row" key={`${k}-${i}`}>
            <dt>{String(k)}</dt>
            <dd>{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
          </div>
        ))}
    </dl>
  );
}

function EmptyState() {
  return (
    <section className="empty-state">
      <ClipboardList size={34} />
      <h2>No advisory loaded</h2>
      <p>Click Load Demo on the left to fetch the apple demo farm and run all engines.</p>
    </section>
  );
}

function LoadingState() {
  return (
    <section className="empty-state">
      <Loader2 className="spin" size={34} />
      <h2>Generating advisory</h2>
      <p>Fetching farm + weather and running stage / fertilizer / pest&disease / yield engines.</p>
    </section>
  );
}

function AdvisoryResult({ result }) {
  const ctx = result.context || {};
  return (
    <div className="result-stack">
      <section className="summary-band">
        <div className="summary-meta">
          {result.farm?.farm_id ? <span>{result.farm.farm_id}</span> : null}
          <span>{ctx.crop || result.resolved_context?.crop}</span>
          <span>Sown {ctx.sowing_date}</span>
          <span>As of {ctx.current_date}</span>
          <span>DAS {ctx.days_after_sowing}</span>
        </div>
        <h2>Apple advisory</h2>
        <p>
          Each card below is a separate engine. Open <i>View inputs that drove this</i> on any
          card to see the raw values the engine reasoned over — current date, DAS, weather bands,
          and any upstream summaries.
        </p>
      </section>

      <section className="engine-grid">
        {ENGINE_CARDS.map((card) => (
          <EngineCard key={card.key} card={card} slot={result[card.key]} />
        ))}
      </section>
    </div>
  );
}

function EngineCard({ card, slot }) {
  const Icon = card.icon;

  if (!slot) {
    if (card.notApplicableWhenMissing) {
      return (
        <details className={`engine-panel ${card.accent} muted`}>
          <summary>
            <span className="engine-title">
              <Icon size={19} />
              <span>
                <b>{card.title}</b>
                <small>{card.notApplicableMessage}</small>
              </span>
            </span>
            <ChevronDown className="chevron" size={18} />
          </summary>
          <div className="engine-content">
            <p className="muted">{card.notApplicableMessage}</p>
          </div>
        </details>
      );
    }
    return null;
  }

  const isError = slot.status === "error";
  const Body = card.key === "pest_disease_cure" ? CureScheduleBody : DefaultEngineBody;

  return (
    <details className={`engine-panel ${card.accent} ${isError ? "errored" : ""}`} open>
      <summary>
        <span className="engine-title">
          <Icon size={19} />
          <span>
            <b>{card.title}</b>
            <small>{card.subtitle}</small>
          </span>
        </span>
        <ChevronDown className="chevron" size={18} />
      </summary>

      <div className="engine-content">
        {isError ? (
          <div className="status-banner banner-error">
            <AlertTriangle size={16} />
            <span>{slot.error?.message || "Engine errored."}</span>
          </div>
        ) : null}

        <p className="engine-copy">{slot.summary}</p>

        {card.key === "fertilizer" ? <SatelliteBlock slot={slot} /> : null}

        {card.key === "pest_disease_risk" ? <RiskOrganisms details={slot.details} /> : null}

        <Body slot={slot} />

        {slot.source_docs?.length ? (
          <div className="source-docs">
            {slot.source_docs.map((s, i) => (
              <span key={i} className="pill pill-source-doc">
                {typeof s === "string" ? s : `${s.doc_key} v${s.version}`}
              </span>
            ))}
          </div>
        ) : null}

        <details className="inputs-drawer">
          <summary>
            <Info size={14} />
            <span>View inputs that drove this</span>
          </summary>
          <InputsDrawerBody inputs={slot.inputs_used} />
        </details>
      </div>
    </details>
  );
}

function SatelliteBlock({ slot }) {
  if (!slot) return null;
  const advisory = slot.satellite_advisory;
  const summary = slot.satellite_summary;
  const d = slot.details || {};
  const ndviHealth = d.ndvi_health;
  const ndviTrend = d.ndvi_trend;
  const ndreStatus = d.ndre_status;
  const inputs = d.satellite_inputs || {};
  if (!advisory && !summary && !ndviHealth && !ndviTrend && !ndreStatus) return null;
  return (
    <div className="satellite-block">
      <div className="satellite-header">
        <Sprout size={14} />
        <strong>Satellite advisory</strong>
        {inputs.source ? <span className="pill pill-source-doc">{inputs.source}</span> : null}
      </div>
      {advisory ? <p className="engine-copy">{advisory}</p> : null}
      {summary ? <p className="reasoning">{summary}</p> : null}
      <div className="satellite-pills">
        {ndviHealth ? <span className="pill">NDVI health: {ndviHealth}</span> : null}
        {ndviTrend ? <span className="pill">NDVI trend: {ndviTrend}</span> : null}
        {ndreStatus ? <span className="pill">NDRE: {ndreStatus}</span> : null}
        {inputs.ndvi_current != null ? (
          <span className="pill">NDVI {inputs.ndvi_current}</span>
        ) : null}
        {inputs.ndvi_delta_7d != null ? (
          <span className="pill">Δ7d {inputs.ndvi_delta_7d}</span>
        ) : null}
        {inputs.ndre_current != null ? (
          <span className="pill">NDRE {inputs.ndre_current}</span>
        ) : null}
      </div>
    </div>
  );
}

function DefaultEngineBody({ slot }) {
  if (!slot.details || Object.keys(slot.details).length === 0) return null;
  // Don't double-render reasoning if it's the only thing in details — the
  // summary already covers the farmer-facing read.
  const keys = Object.keys(slot.details).filter((k) => k !== "reasoning");
  if (keys.length === 0) {
    return slot.details.reasoning ? <p className="reasoning">{slot.details.reasoning}</p> : null;
  }
  return (
    <details className="raw-details">
      <summary>Engine details (JSON)</summary>
      <pre>{JSON.stringify(slot.details, null, 2)}</pre>
    </details>
  );
}

// E4.1 — show the triggered organisms cleanly so the farmer can see WHICH
// organisms the weather is conducive to today, instead of a paragraph.
function RiskOrganisms({ details }) {
  if (!details) return null;
  const triggered = details.triggered_organisms || details.triggered || [];
  const nearMiss = details.near_miss_organisms || details.near_miss || [];
  if (!triggered.length && !nearMiss.length) return null;
  return (
    <div className="risk-organisms">
      {triggered.length ? (
        <div className="risk-block triggered">
          <b>Triggered today</b>
          <ul>
            {triggered.map((o, i) => (
              <li key={i}>
                <span className="organism-name">{o.organism_name || o.name}</span>
                {o.base_risk_pct != null ? <span className="organism-risk">{o.base_risk_pct}% base risk</span> : null}
                {o.drivers?.length ? <span className="organism-drivers">drivers: {o.drivers.join(", ")}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {nearMiss.length ? (
        <div className="risk-block near-miss">
          <b>Near-miss (one band slightly outside)</b>
          <ul>
            {nearMiss.map((o, i) => (
              <li key={i}>
                <span className="organism-name">{o.organism_name || o.name}</span>
                {o.band_outside ? <span className="organism-drivers">{o.band_outside.band} off by {o.band_outside.gap_below ?? o.band_outside.gap_above}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

// E4.2 — render the cure plan as a numbered schedule the farmer can act on.
// Pulls from details.organic_recommendations / details.chemical_recommendations
// (the prompt produces these). If neither is present, fall back to the
// summary text so we never render an empty card.
function CureScheduleBody({ slot }) {
  const details = slot.details || {};
  const organic = details.organic_recommendations || [];
  const chemical = details.chemical_recommendations || [];
  if (!organic.length && !chemical.length) {
    return details.reasoning ? <p className="reasoning">{details.reasoning}</p> : null;
  }
  return (
    <div className="schedule">
      {chemical.length ? (
        <div className="schedule-block">
          <h4>Chemical schedule</h4>
          <ol>
            {chemical.map((row, i) => (
              <ScheduleRow key={`c-${i}`} row={row} />
            ))}
          </ol>
        </div>
      ) : null}
      {organic.length ? (
        <div className="schedule-block">
          <h4>Organic schedule</h4>
          <ol>
            {organic.map((row, i) => (
              <ScheduleRow key={`o-${i}`} row={row} />
            ))}
          </ol>
        </div>
      ) : null}
    </div>
  );
}

function ScheduleRow({ row }) {
  // Each row is "1. <Add | Apply> <material> — <computed_qty>" so the action
  // is always front-loaded. The rate-basis and targets line stays as a
  // smaller secondary line so the farmer can verify against the source CSV.
  const verb = row.action ? "" : "Apply";
  const material = row.material || row.action || "(action)";
  const qty = row.computed_qty || row.quantity || "as directed";
  const targets = row.targets?.length ? `Targets: ${row.targets.join(", ")}` : null;
  return (
    <li>
      <div className="schedule-action">
        {verb ? <span className="verb">{verb}</span> : null}
        <span className="material">{material}</span>
        {qty ? <span className="qty">— {qty}</span> : null}
      </div>
      {row.per_100l_or_per_acre_basis ? (
        <div className="schedule-basis">Rate: {row.per_100l_or_per_acre_basis}</div>
      ) : null}
      {targets ? <div className="schedule-targets">{targets}</div> : null}
    </li>
  );
}

function InputsDrawerBody({ inputs }) {
  if (!inputs) return <p className="muted">No inputs recorded.</p>;
  const flat = [];
  for (const [k, v] of Object.entries(inputs)) {
    if (v == null) continue;
    if (typeof v === "object" && !Array.isArray(v)) {
      flat.push([k, null]);
      for (const [sk, sv] of Object.entries(v)) {
        if (sv == null) continue;
        flat.push([`  · ${sk}`, typeof sv === "object" ? JSON.stringify(sv) : String(sv)]);
      }
    } else {
      flat.push([k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
    }
  }
  return (
    <dl className="kv-list inputs-list">
      {flat.map(([k, v], i) =>
        v === null ? (
          <div className="kv-row kv-section" key={i}>
            <dt>{k}</dt>
          </div>
        ) : (
          <div className="kv-row" key={i}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ),
      )}
    </dl>
  );
}

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, info: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    console.error("UI render error:", error, info);
    this.setState({ info });
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: "monospace", color: "#7b1f1f" }}>
          <h2 style={{ marginBottom: 8 }}>UI render error</h2>
          <pre style={{ whiteSpace: "pre-wrap", background: "#fdecec", padding: 12, borderRadius: 6 }}>
            {String(this.state.error?.stack || this.state.error)}
          </pre>
          {this.state.info?.componentStack ? (
            <pre style={{ whiteSpace: "pre-wrap", background: "#fff7e0", padding: 12, borderRadius: 6, marginTop: 8 }}>
              {this.state.info.componentStack}
            </pre>
          ) : null}
          <p style={{ marginTop: 12 }}>Open the browser DevTools Console for more detail. The advisory data did load — only rendering failed.</p>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);
