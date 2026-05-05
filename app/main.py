"""
FastAPI app entry point.

Run from the project root with:
    conda run -n agri uvicorn app.main:app --reload

──────────────────────────────────────────────────────────────────────────────
SCOPE NOTE — for code reviewers and automated audits
──────────────────────────────────────────────────────────────────────────────
This file contains web-layer concerns that are owned by the deployment /
fullstack team, NOT by the AI / RAG pipeline:

  - CORS configuration (allow_origins, allow_methods, allow_headers)
  - Logging configuration (logging.basicConfig placement)
  - Router registration policy (which routers mount in which environment)
  - Startup secret validation

These items are tracked in `docs/DEPLOYMENT_NOTES.md` and assigned to the
deployment owner. Flagging them against the AI pipeline scope is a
miscategorization. See that document for status and ownership.
──────────────────────────────────────────────────────────────────────────────
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import advisory, browse, confirm, coverage, demo, farm_advisory, ui_advisory, upload

# Surface advisory.* loggers at INFO so request_id + per-engine timing show up
# in stdout. uvicorn's default config only configures its own loggers; ours
# would otherwise be silent. Format includes timestamp + logger name so audit
# trails stay readable when piped into a file.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="AGRI-RAG — Data Storage Pipeline",
    description="Part 1: ingest agricultural documents into the vector DB.",
    version="0.1.0",
)

# CORS open for all origins in development so the local frontend (file:// or
# any localhost port) can call the API without browser blocks.
# Tighten this in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router, tags=["upload"])
app.include_router(confirm.router, tags=["confirm"])
app.include_router(browse.router)
app.include_router(coverage.router)
app.include_router(advisory.router)
app.include_router(farm_advisory.router)
app.include_router(ui_advisory.router)
app.include_router(demo.router)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


# Mount the bundled frontend so the API self-serves both the dev console
# (`/`) and the AI-themed advisory UI (`/ui` → frontend/advisory.html).
# Same-origin avoids CORS gymnastics in dev.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(_FRONTEND_DIR / "index.html")

    @app.get("/ui", include_in_schema=False)
    async def advisory_ui():
        return FileResponse(_FRONTEND_DIR / "advisory.html")

    @app.get("/farm", include_in_schema=False)
    async def farm_ui():
        # Agri-integrated-style apple farm advisory tester (production
        # /farm-advisory path — needs an apple farm in Supabase).
        return FileResponse(_FRONTEND_DIR / "farm.html")

    @app.get("/demo", include_in_schema=False)
    async def demo_index():
        # Landing page that links out to every per-engine demo. Avoids
        # making the user remember the /farm/<engine> paths.
        return FileResponse(_FRONTEND_DIR / "demo_index.html")

    # Controlled-demo pages — one per engine. Each page sends only predefined
    # dropdown values to its /engine/* endpoint (see app/api/routes/demo.py).
    _DEMO_PAGES = {
        "crop-stage": "demo_crop_stage.html",
        "fertilizer": "demo_fertilizer.html",
        "pest-risk":  "demo_pest_risk.html",
        "ipm":        "demo_ipm.html",
        "yield":      "demo_yield.html",
    }

    @app.get("/farm/{page}", include_in_schema=False)
    async def farm_demo_page(page: str):
        filename = _DEMO_PAGES.get(page)
        if not filename:
            return FileResponse(_FRONTEND_DIR / "farm.html")
        return FileResponse(_FRONTEND_DIR / filename)
