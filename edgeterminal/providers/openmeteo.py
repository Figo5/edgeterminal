"""Open-Meteo weather. Free, no API key, no attribution requirement."""
from __future__ import annotations

import datetime as dt
import time
from typing import Dict, Optional

from .base import ProviderError, WeatherProvider, get_json

URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
CODES = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
         45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
         61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Light snow", 73: "Snow",
         75: "Heavy snow", 80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
         95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail"}
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


class OpenMeteo(WeatherProvider):
    name = "open-meteo"
    _locations: Dict[str, tuple] = {}

    def geocode(self, city: str, state: Optional[str] = None) -> tuple:
        key = f"{city}|{state or ''}".lower()
        hit = self._locations.get(key)
        if hit:
            return hit
        data = get_json(GEOCODE_URL, {"name": city, "count": 10, "language": "en",
                                     "format": "json"}, provider="open-meteo-geocoding")
        results = (data or {}).get("results") or []
        if state:
            preferred = [r for r in results if str(r.get("admin1", "")).lower() == state.lower()]
            results = preferred or results
        if not results:
            raise ProviderError(self.name, f"venue city not found: {city}, {state or ''}")
        loc = (float(results[0]["latitude"]), float(results[0]["longitude"]))
        self._locations[key] = loc
        return loc

    def forecast(self, lat: float, lon: float, when_iso: Optional[str] = None) -> Dict:
        data = get_json(URL, {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m,"
                      "wind_direction_10m,weather_code",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "forecast_days": 7, "timezone": "UTC"}, provider=self.name)
        hourly = (data or {}).get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            raise ProviderError(self.name, "empty forecast payload")
        idx = 0
        if when_iso:
            try:
                target = dt.datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
                target = target.astimezone(dt.timezone.utc).replace(tzinfo=None)
                idx = min(range(len(times)),
                          key=lambda i: abs(dt.datetime.fromisoformat(times[i]) - target))
            except Exception:  # noqa: BLE001 - fall back to first hour
                idx = 0
        wd = (hourly.get("wind_direction_10m") or [None])[idx]
        return dict(
            temp_f=(hourly.get("temperature_2m") or [None])[idx],
            wind_mph=(hourly.get("wind_speed_10m") or [None])[idx],
            wind_gust_mph=(hourly.get("wind_gusts_10m") or [None])[idx],
            wind_dir=DIRS[int((wd % 360) / 22.5)] if wd is not None else None,
            precip_chance=(hourly.get("precipitation_probability") or [None])[idx],
            conditions=CODES.get((hourly.get("weather_code") or [None])[idx], "Unknown"),
            indoor=False, source=self.name, retrieved_at=time.time(),
            valid_for=times[idx] + "Z")
