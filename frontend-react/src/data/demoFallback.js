// Local fallback used only if /ui-advisory/demo/{farm_id} is unreachable.
// The real demo is supposed to come from Supabase apple_demo_003 (tomorrow's
// main focus). This shape mirrors what the API returns so the UI can be
// developed against it without the backend running.
export const demoFallback = {
  farm: {
    farm_id: "APPLE_DEMO_003",
    farm_name: "Demo Apple Orchard",
    crop: "apple",
    sowing_date: "2024-03-01",
    expected_harvest_date: "2026-09-15",
    language: "English",
    location: { latitude: 31.1, longitude: 77.17, district: "Shimla", state: "Himachal Pradesh", country: "India" },
    irrigation_method: "Drip",
    farm_area_acres: 1,
    tree_count: 200,
  },
  resolved_context: {
    source: "fallback",
    crop: "apple",
    sowing_date: "2024-03-01",
    current_date: "2026-05-02",
    days_after_sowing: 793,
    weather: { temperature_c: 18, humidity_pct: 88, conducive_duration_hrs: 10, rainfall_mm: 2 },
    extra: { tree_count: 200, farm_area_acres: 1 },
  },
  request_id: "demo-fallback",
  context: { crop: "apple", days_after_sowing: 793, sowing_date: "2024-03-01", current_date: "2026-05-02" },
  stage: {
    summary: "Apple is in petal-fall / early fruit-set in Himachal at this calendar date.",
    details: { reasoning: "Calendar window for petal fall in north-Indian apple regions." },
    inputs_used: { crop: "apple", current_date: "2026-05-02", days_after_sowing: 793 },
    status: "ok",
  },
  fertilizer: {
    summary: "Apply post-petal-fall N split per fertigation schedule.",
    details: { reasoning: "Fertigation schedule entry for petal fall." },
    inputs_used: { crop: "apple", days_after_sowing: 793 },
    status: "ok",
  },
  pest_disease_risk: {
    summary: "Apple Scab triggered: 18°C / 88% RH / 10h leaf wetness lands inside the rule band.",
    details: {
      reasoning: "Live weather satisfies all three Apple Scab bands for the petal-fall stage.",
      triggered_organisms: [
        { organism_name: "Apple Scab", base_risk_pct: 70, drivers: ["humidity", "leaf_wetness"] },
      ],
      near_miss_organisms: [],
    },
    inputs_used: { weather: { temperature_c: 18, humidity_pct: 88, conducive_duration_hrs: 10 }, days_after_sowing: 793 },
    status: "ok",
  },
  pest_disease_cure: {
    summary: "For your 200-tree orchard in petal-fall, mix 5 kg Mancozeb in 2,000 L water.",
    details: {
      reasoning: "IPM schedule petal-fall block; total spray volume = 200 trees × 10 L = 2000 L.",
      organic_recommendations: [
        { material: "Neem oil", computed_qty: "10 L total", per_100l_or_per_acre_basis: "0.5 L per 100 L", targets: ["aphids"] },
      ],
      chemical_recommendations: [
        { material: "Mancozeb 75% WP", computed_qty: "5 kg total in 2000 L water", per_100l_or_per_acre_basis: "250 g per 100 L", targets: ["Apple Scab"] },
      ],
    },
    inputs_used: { farm: { tree_count: 200, farm_area_acres: 1 }, days_after_sowing: 793 },
    status: "ok",
  },
  yield: {
    summary: "Yield outlook is moderate-to-healthy for a mature 200-tree orchard.",
    details: { reasoning: "Yield parameters doc, mature-tree band." },
    inputs_used: { crop: "apple", days_after_sowing: 793 },
    status: "ok",
  },
};
