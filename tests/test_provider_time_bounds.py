"""Provider timestamps and forecast windows must mean what the UI claims."""
import pytest

from edgeterminal.providers import espn, openmeteo
from edgeterminal.providers.base import ProviderError


def test_openmeteo_rejects_forecast_outside_kickoff_hour(monkeypatch):
    payload = {"hourly": {"time": ["2026-09-23T10:00"],
                          "temperature_2m": [65], "wind_speed_10m": [4],
                          "wind_gusts_10m": [6], "wind_direction_10m": [90],
                          "precipitation_probability": [0], "weather_code": [0]}}
    monkeypatch.setattr(openmeteo, "get_json", lambda *args, **kwargs: payload)
    provider = openmeteo.OpenMeteo()
    assert provider.forecast(1, 2, "2026-09-23T10:00:00Z")["valid_for"] == \
           "2026-09-23T10:00Z"
    with pytest.raises(ProviderError, match="does not cover kickoff"):
        provider.forecast(1, 2, "2026-09-25T10:00:00Z")


def test_cached_injuries_keep_original_fetch_timestamp(monkeypatch):
    payload = {"injuries": [{"displayName": "Green Bay Packers", "injuries": [
        {"athlete": {"displayName": "Example Player"}, "status": "Questionable"}]}]}
    monkeypatch.setattr(espn, "_cached", lambda *args, **kwargs: payload)
    monkeypatch.setitem(espn._cache, "inj:NFL", (123.0, payload))
    rows = espn.EspnInjuries().injuries("NFL")
    assert rows[0]["retrieved_at"] == 123.0
