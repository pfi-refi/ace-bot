"""
OpenWeatherMap proxy for the Ace Portal status bar.

The API key stays server-side (OPENWEATHER_API_KEY env var) — never sent to the
browser. Cleveland, OH coordinates per the build spec.
"""

import logging
import os

import httpx

logger = logging.getLogger("ace_portal.weather")

# Cleveland, OH (per build spec)
CLE_LAT = 41.3961
CLE_LON = -81.4399
CLE_LABEL = "CLEVELAND, OH"


async def get_weather() -> dict:
    """Return current Cleveland conditions. Shape is always stable so the UI can
    render a graceful placeholder when the key is missing or the API errors."""
    api_key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    base = {
        "ok": False,
        "location": CLE_LABEL,
        "temp": None,
        "condition": "",
        "icon": "",          # OpenWeather icon code (e.g. "01d")
        "high": None,
        "low": None,
        "humidity": None,
        "wind": None,
    }
    if not api_key:
        base["condition"] = "NO KEY"
        return base
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": CLE_LAT,
        "lon": CLE_LON,
        "units": "imperial",
        "appid": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        weather0 = (data.get("weather") or [{}])[0]
        main = data.get("main", {})
        base.update({
            "ok": True,
            "temp": round(main.get("temp")) if main.get("temp") is not None else None,
            "condition": (weather0.get("main") or "").upper(),
            "description": (weather0.get("description") or "").title(),
            "icon": weather0.get("icon", ""),
            "high": round(main.get("temp_max")) if main.get("temp_max") is not None else None,
            "low": round(main.get("temp_min")) if main.get("temp_min") is not None else None,
            "humidity": main.get("humidity"),
            "wind": round((data.get("wind", {}) or {}).get("speed", 0)),
        })
        return base
    except Exception as e:
        logger.error("Weather fetch error: %s", e)
        base["condition"] = "UNAVAILABLE"
        return base
