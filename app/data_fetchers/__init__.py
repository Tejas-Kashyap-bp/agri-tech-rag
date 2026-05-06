"""
Data-fetcher facade.

The fetchers (Supabase farm registry, Open-Meteo weather, SoilGrids soil
pipeline, Sentinel Hub satellite) are vendored under `app/data_fetchers/_vendor/`
so this repository runs without any sibling-repo dependency.
"""

from ._vendor.user import get_farm_profile
from ._vendor.weather import get_weather_features
from ._vendor.soil.get_soil_data import get_soil_data

__all__ = ["get_farm_profile", "get_weather_features", "get_soil_data"]
