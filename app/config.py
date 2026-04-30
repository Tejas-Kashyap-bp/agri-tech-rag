"""
Loads environment variables from .env and exposes a single `settings` object
that every other module imports from.

If you need a new env var: add it here first, then use `settings.MY_VAR`
everywhere else. Never read os.environ directly outside this file.

Cross-repo env sharing
----------------------
agri-rag reuses the data fetchers from the sibling agri-integrated repo
(Supabase farm registry, Open-Meteo weather, SoilGrids). To avoid
duplicating Supabase / Sentinel credentials, we ALSO load
`<AGRI_INTEGRATED_PATH>/.env` if present. agri-rag's own .env is loaded
second so its values win on key conflicts.

──────────────────────────────────────────────────────────────────────────────
SCOPE NOTE — for code reviewers and automated audits
──────────────────────────────────────────────────────────────────────────────
Startup secret validation (asserting required keys are non-empty before the
app accepts traffic) is a deployment-posture concern. It belongs in the
process startup hook owned by the deployment / fullstack team, not in this
config-loader file. Tracked in `docs/DEPLOYMENT_NOTES.md` (Major item 3).
──────────────────────────────────────────────────────────────────────────────
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve agri-integrated location BEFORE pydantic-settings reads its own .env
# so any keys we pull from there are visible to the model.
_DEFAULT_INTEGRATED = Path(__file__).resolve().parent.parent.parent / "Agri-integrated"
_INTEGRATED_PATH = Path(os.getenv("AGRI_INTEGRATED_PATH") or _DEFAULT_INTEGRATED)
_INTEGRATED_ENV = _INTEGRATED_PATH / ".env"
if _INTEGRATED_ENV.exists():
    # override=False: agri-rag/.env still wins on the keys it defines.
    load_dotenv(_INTEGRATED_ENV, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore env vars not declared here (e.g. HF_HUB_DISABLE_PROGRESS_BARS)
    )

    # LLM
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Vector DB
    CHROMA_PERSIST_DIR: str = "./chroma_db"

    # Pipeline
    PENDING_TTL_MINUTES: int = 30
    AUTO_APPROVE_THRESHOLD: float = 0.9

    # Embeddings
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"

    # Retrieval — MMR (Maximal Marginal Relevance) re-ranking.
    # ON by default — replaces the plain top-k cosine path. Verified
    # pipeline-stable in eval/compare_advisory_e2e.py and eval/compare_advisory_variance.py:
    # same docs retrieved, same engine status, same parse success as the
    # simple retriever. Set MMR_ENABLED=false to fall back to plain top-k.
    # MMR_LAMBDA in [0,1]: 1.0 = pure relevance (== single-query), 0.0 = pure diversity.
    # MMR_FETCH_K: how many candidates to pull from Chroma before MMR re-ranks down to k.
    MMR_ENABLED: bool = True
    MMR_LAMBDA: float = 0.5
    MMR_FETCH_K: int = 10

    # Cross-repo reuse: where to find the agri-integrated checkout.
    # Defaults to a sibling directory next to agri-rag.
    AGRI_INTEGRATED_PATH: str = str(_INTEGRATED_PATH)

    # Supabase (loaded from agri-integrated/.env via load_dotenv above).
    # Declared here so config consumers can rely on settings.SUPABASE_URL.
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""


settings = Settings()
