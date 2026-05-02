import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API calls to the FastAPI backend on :8000 so the React
// app can call /ui-advisory without CORS / hardcoded base URLs. In prod the
// build is served from the same origin (or VITE_API_BASE_URL is set).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ui-advisory": "http://localhost:8000",
      "/advisory": "http://localhost:8000",
      "/farm-advisory": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
