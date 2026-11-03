"""Daily mean temperatures from Open-Meteo (free, no API key).

Used for heating degree days (HDD), which drive gas consumption. Past days come
from the archive API, recent and coming days from the forecast API. The location
is rounded to 0.1 degree (about 10 km) before it is sent.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import aiohttp

_LOGGER = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEZONE = "Europe/Brussels"

# Belgian convention for heating degree days (graaddagen / degrés-jours 16,5)
HDD_BASE = 16.5
# Years of history used for the "normal weather" spread
CLIMATE_YEARS = 10
# The archive lags a few days behind; the forecast API covers that gap
ARCHIVE_DELAY_DAYS = 6
FORECAST_PAST_DAYS = 14
FORECAST_DAYS = 16


def hdd(temperature: float) -> float:
    return max(0.0, HDD_BASE - temperature)


async def _daily_means(session: aiohttp.ClientSession, url: str, params: dict) -> dict[date, float]:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    daily = data.get("daily") or {}
    return {
        date.fromisoformat(day): temp
        for day, temp in zip(daily.get("time", []), daily.get("temperature_2m_mean", []))
        if temp is not None
    }


async def async_fetch_temperatures(
    session: aiohttp.ClientSession, latitude: float, longitude: float, today: date
) -> dict[date, float]:
    """Daily mean temperatures from 1 January CLIMATE_YEARS+1 years ago to today + 15 days."""
    location = {
        "latitude": round(latitude, 1),
        "longitude": round(longitude, 1),
        "daily": "temperature_2m_mean",
        "timezone": TIMEZONE,
    }
    archive = await _daily_means(session, ARCHIVE_URL, {
        **location,
        "start_date": date(today.year - CLIMATE_YEARS - 1, 1, 1).isoformat(),
        "end_date": (today - timedelta(days=ARCHIVE_DELAY_DAYS)).isoformat(),
    })
    forecast = await _daily_means(session, FORECAST_URL, {
        **location,
        "past_days": FORECAST_PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
    })
    # Archive values (reanalysis) take precedence for days both sources cover
    return {**forecast, **archive}
