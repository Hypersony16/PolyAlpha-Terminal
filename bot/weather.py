import statistics
from datetime import date

import httpx

from bot.cache import cache
from bot.config import CITIES, ENSEMBLE_MODEL, FORECAST_DAYS, WEATHER_CACHE_TTL


def _city_coords(city: str):
    cfg = CITIES.get(city.lower()) or CITIES.get("munich")
    return cfg[1], cfg[2]


async def fetch_weather_model(city: str = "munich") -> dict:
    target_date = date.today().isoformat()
    cache_key = f"weather_model:{city}:{target_date}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    lat, lon = _city_coords(city)

    # Try ensemble first
    try:
        url = "https://ensemble-api.open-meteo.com/v1/ensemble"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "models": ENSEMBLE_MODEL,
            "forecast_days": FORECAST_DAYS,
            "timezone": "Europe/Berlin",
        }
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        idxs = [i for i, t in enumerate(times) if str(t).startswith(target_date)]
        member_highs = []
        if idxs:
            start, end = min(idxs), max(idxs) + 1
            for key, series in hourly.items():
                if key == "time" or "temperature_2m" not in key:
                    continue
                if isinstance(series, list) and len(series) >= end:
                    vals = [float(v) for v in series[start:end] if v is not None]
                    if vals:
                        member_highs.append(max(vals))

        if member_highs:
            consensus = round(statistics.mean(member_highs), 2)
            result = {
                "date": target_date,
                "high": consensus,
                "consensus_high": consensus,
                "source": "ensemble",
                "member_highs": member_highs,
                "spread": round(statistics.stdev(member_highs), 2) if len(member_highs) > 1 else 0.0,
            }
            cache.set(cache_key, result, ttl_seconds=WEATHER_CACHE_TTL)
            return result
    except Exception:
        pass

    # Deterministic fallback
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "forecast_days": FORECAST_DAYS,
        "timezone": "Europe/Berlin",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    if target_date in dates:
        high = float(highs[dates.index(target_date)])
    elif highs:
        high = float(highs[0])
    else:
        high = 15.0

    result = {
        "date": target_date,
        "high": high,
        "consensus_high": high,
        "source": "forecast",
        "member_highs": [],
        "spread": 1.5,
    }
    cache.set(cache_key, result, ttl_seconds=WEATHER_CACHE_TTL)
    return result


# Claude compatibility helpers
async def fetch_weather(city: str, target_date: str | None = None):
    return await fetch_weather_model(city)


async def fetch_weather_multi(city: str, days: int = 3):
    return [await fetch_weather_model(city)]
