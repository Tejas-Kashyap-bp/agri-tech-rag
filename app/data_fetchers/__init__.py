"""
Cross-repo bridge to agri-integrated's data fetchers.

WHY this is a thin re-export shim (not a copy):
  agri-integrated owns the production fetcher code (Supabase farm registry,
  Open-Meteo weather, SoilGrids soil pipeline, Sentinel Hub satellite). We
  reuse it directly so:
    - one source of truth for farm data
    - bug fixes / schema changes in agri-integrated automatically propagate
    - we don't accumulate a second copy that drifts

How it works:
  At import time we splice the agri-integrated checkout onto sys.path so
  `import data_fetchers.user` resolves over there. The path is configured by
  settings.AGRI_INTEGRATED_PATH (defaults to a sibling directory).

Failure mode:
  If the path isn't a valid checkout, individual symbol imports below raise
  ImportError on first use. We don't fail at module-import time so unrelated
  parts of the API (e.g. /upload, /advisory with raw context) still work.
"""

import sys
from pathlib import Path

from app.config import settings

_INTEGRATED = Path(settings.AGRI_INTEGRATED_PATH).resolve()
if _INTEGRATED.exists() and str(_INTEGRATED) not in sys.path:
    sys.path.insert(0, str(_INTEGRATED))

# The soil sub-package uses bare `from models import ...` imports that assume
# its own directory is on sys.path. Add it so get_soil_data() works. Also add
# the layers dir for the same reason (its submodules import sibling layers).
_SOIL_DIR = _INTEGRATED / "data_fetchers" / "soil"
for _p in (_SOIL_DIR, _SOIL_DIR / "layers"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def get_farm_profile(farm_id: str) -> dict:
    """Lazy re-export — defers ImportError to call time."""
    from data_fetchers.user import get_farm_profile as _impl  # type: ignore
    return _impl(farm_id)


def get_weather_features(latitude: float, longitude: float) -> dict:
    from data_fetchers.weather import get_weather_features as _impl  # type: ignore
    return _impl(latitude, longitude)


def get_soil_data(**kwargs):
    """
    Returns a SoilData object (call .to_dict() on it). Kwargs match
    agri-integrated/data_fetchers/soil/get_soil_data.py:get_soil_data.
    """
    from data_fetchers.soil.get_soil_data import get_soil_data as _impl  # type: ignore
    return _impl(**kwargs)


__all__ = ["get_farm_profile", "get_weather_features", "get_soil_data"]
