# AGRI-RAG — How to Run

> **Scope note for reviewers:** This repository implements the AI / RAG
> pipeline (ingestion + advisory). Web-layer concerns — CORS, authentication,
> request size limits, secret validation, logging configuration, deployment
> posture — are owned by the deployment / fullstack team and tracked in
> [`docs/DEPLOYMENT_NOTES.md`](docs/DEPLOYMENT_NOTES.md). Items in that
> document are intentionally out of scope for the AI pipeline.

This project has two parts you need to start:
1. **Backend** — FastAPI app (the brain, runs on port `8000`)
2. **Frontend** — a single `index.html` page (the UI, runs on port `5500`)

You need **both** running at the same time, in **two separate terminals**.

---

## 1. Run the Backend (FastAPI)

Open a terminal in the project root (`agri-rag/`) and run:

```bash
conda run -n agri uvicorn app.main:app --reload --port 8000
```

What this does:
- `conda run -n agri` → uses the conda environment named `agri`
- `uvicorn app.main:app` → starts the FastAPI app defined in `app/main.py`
- `--reload` → auto-restarts when you change Python files
- `--port 8000` → serves on http://localhost:8000

You should see something like `Uvicorn running on http://127.0.0.1:8000`.

To check it's working, open in a browser:
- http://localhost:8000/docs — interactive Swagger UI for the API

**Leave this terminal open.** Closing it stops the backend.

---

## 2. Run the Frontend (UI)

Open a **second terminal**, go into the `frontend/` folder, and start a simple static server:

```bash
cd frontend
python3 -m http.server 5500
```

What this does:
- Serves the files in `frontend/` over HTTP
- `5500` is just the port number (any free port works)

Then open in a browser:
- http://localhost:5500/index.html

The UI will talk to the backend at `http://127.0.0.1:8000` automatically.

**Leave this terminal open** as well.

---

## Stopping

In each terminal, press `Ctrl + C` to stop the server.

---

## Troubleshooting

- **"conda: command not found"** → install Miniconda/Anaconda, or activate the env manually: `conda activate agri` then run `uvicorn app.main:app --reload --port 8000`.
- **"Make sure the server is running on port 8000"** (shown in the UI) → the backend is not running. Start it (step 1).
- **Port already in use** → either kill the old process or pick a different port (e.g. `--port 8001`). If you change the backend port, update `const API` in `frontend/index.html` to match.
- **CORS errors in browser console** → make sure you're opening the UI through `http://localhost:5500/...`, not by double-clicking the HTML file.

---

## Quick Helper Script (optional)

To upload one file and auto-approve all confirmation steps from the command line:

```bash
./ingest_helper.sh path/to/file.pdf
```

This hits the same backend the UI uses — handy for bulk-loading data.
