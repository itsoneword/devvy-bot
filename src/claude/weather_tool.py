"""Weather lookup for agents — backs the ``get_weather`` in-process MCP tool.

Uses Open-Meteo (https://open-meteo.com): free, keyless, no rate-limit signup.
Geocoding resolves a place name to coordinates; the forecast endpoint covers
today plus ~16 days ahead, the archive (ERA5 reanalysis) endpoint covers
everything from 1940 up to a few days ago. Together they answer "what was/will
the temperature be on date X in place Y" for any date a sleep/recovery diary
entry would plausibly reference.

No API key, no daemon, no MCP HTTP seam needed — this runs in-process (see
``agent_tools.py`` docstring) so it works even from sandboxed profiles whose
Bash tool has no network access.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import httpx
import structlog

logger = structlog.get_logger()

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# The user's home base — used whenever the model omits a location (e.g. a
# routine sleep-diary entry with no travel mentioned). Not a hard default for
# every profile forever, just a sane fallback for this single-user bot.
DEFAULT_LOCATION = "Costa de Caparica, Portugal"
DEFAULT_LAT = 38.6489
DEFAULT_LON = -9.2373

_HOURS_OF_INTEREST = [
    "00:00",
    "03:00",
    "06:00",
    "09:00",
    "12:00",
    "15:00",
    "18:00",
    "21:00",
]

# Tiny in-process cache so repeated diary-entry turns don't re-geocode the
# same handful of place names every time.
_geocode_cache: Dict[str, Tuple[float, float, str]] = {}

_HTTP_TIMEOUT = 10.0


async def _geocode(location: str) -> Tuple[float, float, str]:
    """Resolve a place name to (lat, lon, resolved_display_name)."""
    key = location.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(
            GEOCODING_URL, params={"name": location, "count": 1, "language": "en"}
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") or []
    if not results:
        raise ValueError(f"no location found for {location!r}")
    r = results[0]
    name_parts = [r.get("name")]
    if r.get("admin1"):
        name_parts.append(r["admin1"])
    if r.get("country"):
        name_parts.append(r["country"])
    resolved = ", ".join(p for p in name_parts if p)
    result = (float(r["latitude"]), float(r["longitude"]), resolved)
    _geocode_cache[key] = result
    return result


def _parse_date(value: str) -> date_cls:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def get_weather_for(
    location: Optional[str], date_str: Optional[str]
) -> Dict[str, Any]:
    """Fetch day/night temperature for a location + date.

    ``date_str`` is ``YYYY-MM-DD``; defaults to today. Dates more than 2 days
    in the past use the historical archive endpoint, everything else (today,
    yesterday, up to ~16 days ahead) uses the forecast endpoint.
    """
    location = (location or "").strip()
    if location and location.lower() != DEFAULT_LOCATION.lower():
        lat, lon, resolved_name = await _geocode(location)
    else:
        lat, lon, resolved_name = DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION

    today = datetime.now().date()
    target = _parse_date(date_str) if date_str else today

    base_url = ARCHIVE_URL if (today - target).days > 2 else FORECAST_URL
    params: Dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(base_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    daily = data.get("daily") or {}
    day_max = (daily.get("temperature_2m_max") or [None])[0]
    day_min = (daily.get("temperature_2m_min") or [None])[0]

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    by_hour = dict(zip([t[11:16] for t in times], temps))
    hourly_of_interest = {h: by_hour[h] for h in _HOURS_OF_INTEREST if h in by_hour}

    return {
        "location": resolved_name,
        "latitude": lat,
        "longitude": lon,
        "date": target.isoformat(),
        "source": "archive" if base_url == ARCHIVE_URL else "forecast",
        "day_max_c": day_max,
        "night_min_c": day_min,
        "hourly_c": hourly_of_interest,
    }
