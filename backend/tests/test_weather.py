"""Weather provider: Open-Meteo forecast vs. climatology, window summaries, failures, mock."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.providers.base import GeoPoint
from app.providers.weather import (
    ARCHIVE_URL,
    FORECAST_URL,
    MockWeatherProvider,
    OpenMeteoWeatherProvider,
    build_weather_provider,
)

AUSTIN = GeoPoint(lat=30.27, lng=-97.74)
TODAY = date(2026, 11, 9)


def today() -> date:
    return TODAY


def forecast_payload(day: date, *, hourly: bool = False) -> dict:
    payload = {
        "timezone": "America/Chicago",
        "utc_offset_seconds": -21600,
        "daily": {
            "time": [day.isoformat()],
            "weather_code": [3],
            "temperature_2m_max": [24.8],
            "temperature_2m_min": [16.7],
            "precipitation_probability_max": [35],
            "precipitation_sum": [2.4],
            "wind_speed_10m_max": [14.8],
        },
    }
    if hourly:
        hours = list(range(24))
        payload["hourly"] = {
            "time": [f"{day.isoformat()}T{h:02d}:00" for h in hours],
            "temperature_2m": [15 + h / 2 for h in hours],
            "precipitation_probability": [60 if h == 19 else 10 for h in hours],
            "precipitation": [1.2 if h == 19 else 0.0 for h in hours],
            "weather_code": [63 if h == 19 else 2 for h in hours],
            "wind_speed_10m": [8 + (h % 5) for h in hours],
        }
    return payload


def archive_payload(start: date, *, wet_days: int = 2, hourly: bool = False) -> dict:
    """Seven archive days starting at `start`; the first `wet_days` of them get 8 mm of rain."""
    days = [start + timedelta(days=i) for i in range(7)]
    payload = {
        "timezone": "America/Chicago",
        "daily": {
            "time": [d.isoformat() for d in days],
            "temperature_2m_max": [30.0 + i for i in range(7)],
            "temperature_2m_min": [18.0 + i for i in range(7)],
            "precipitation_sum": [8.0 if i < wet_days else 0.0 for i in range(7)],
            "wind_speed_10m_max": [20.0] * 7,
        },
    }
    if hourly:
        stamps, temps, rain = [], [], []
        for i, d in enumerate(days):
            for h in range(24):
                stamps.append(f"{d.isoformat()}T{h:02d}:00")
                temps.append(10.0 + h / 2)
                rain.append(2.0 if (i < wet_days and h == 20) else 0.0)
        payload["hourly"] = {"time": stamps, "temperature_2m": temps, "precipitation": rain}
    return payload


def archive_route(respx_mock, **kwargs):
    def respond(request: httpx.Request) -> httpx.Response:
        start = date.fromisoformat(request.url.params["start_date"])
        return httpx.Response(200, json=archive_payload(start, **kwargs))

    return respx_mock.get(ARCHIVE_URL).mock(side_effect=respond)


# --- forecast ------------------------------------------------------------------------------------


@pytest.mark.respx(assert_all_mocked=True)
async def test_day_in_horizon_uses_forecast_api(respx_mock):
    day = TODAY + timedelta(days=3)
    route = respx_mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload(day)))
    provider = OpenMeteoWeatherProvider(today=today)

    outlook = await provider.get_outlook(AUSTIN, day)

    assert outlook is not None and outlook.basis == "forecast" and outlook.day == day
    params = route.calls.last.request.url.params
    assert params["start_date"] == params["end_date"] == day.isoformat()
    assert params["timezone"] == "auto" and "hourly" not in params
    for var in ("temperature_2m_max", "precipitation_probability_max", "weather_code", "wind_speed_10m_max"):
        assert var in params["daily"]
    assert (outlook.temp_high_c, outlook.temp_low_c) == (24.8, 16.7)
    assert outlook.precipitation_probability == 0.35 and outlook.precipitation_mm == 2.4 and outlook.wind_kph == 14.8
    assert "overcast" in outlook.summary and "35% chance" in outlook.summary and "Forecast for" in outlook.summary
    assert outlook.window_summary is None


async def test_forecast_window_summary_describes_requested_hours(respx_mock):
    day = TODAY + timedelta(days=1)
    route = respx_mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload(day, hourly=True)))

    outlook = await OpenMeteoWeatherProvider(today=today).get_outlook(AUSTIN, day, start_hour=18, end_hour=22)

    assert "hourly" in route.calls.last.request.url.params
    assert outlook is not None and outlook.window_summary is not None
    assert outlook.window_summary.startswith("18:00–22:00:")
    # Hours 18..21: temps 24..25.5, dominant code partly cloudy with moderate rain at 19:00, max probability 60%.
    assert "partly cloudy, moderate rain at times" in outlook.window_summary
    assert "24–26°C" in outlook.window_summary and "60% chance" in outlook.window_summary


async def test_forecast_horizon_edges(respx_mock):
    forecast = respx_mock.get(FORECAST_URL).mock(
        side_effect=lambda req: httpx.Response(
            200, json=forecast_payload(date.fromisoformat(req.url.params["start_date"]))
        )
    )
    archive_route(respx_mock)
    provider = OpenMeteoWeatherProvider(today=today)

    last_forecast_day = await provider.get_outlook(AUSTIN, TODAY + timedelta(days=15))
    first_climatology_day = await provider.get_outlook(AUSTIN, TODAY + timedelta(days=16))

    assert last_forecast_day is not None and last_forecast_day.basis == "forecast"
    assert first_climatology_day is not None and first_climatology_day.basis == "climatology"
    assert forecast.call_count == 1


# --- climatology ---------------------------------------------------------------------------------


async def test_far_future_day_builds_climatology_from_archive(respx_mock):
    archive = archive_route(respx_mock, wet_days=2)
    forecast = respx_mock.get(FORECAST_URL)
    day = date(2027, 6, 12)

    outlook = await OpenMeteoWeatherProvider(today=today).get_outlook(AUSTIN, day)

    assert not forecast.called
    assert archive.call_count == 10
    windows = sorted((c.request.url.params["start_date"], c.request.url.params["end_date"]) for c in archive.calls)
    assert windows[0] == ("2017-06-09", "2017-06-15") and windows[-1] == ("2026-06-09", "2026-06-15")
    assert all(c.request.url.params["timezone"] == "auto" for c in archive.calls)
    assert outlook is not None and outlook.basis == "climatology" and outlook.day == day
    assert outlook.temp_high_c == 33.0 and outlook.temp_low_c == 21.0  # means of 30..36 and 18..24
    assert outlook.precipitation_probability == pytest.approx(2 / 7, abs=0.01)
    assert outlook.precipitation_mm == pytest.approx(16 / 7, abs=0.1) and outlook.wind_kph == 20.0
    assert "not a forecast" in outlook.summary and "2017–2026" in outlook.summary
    assert "29% of days" in outlook.summary and "wet days average 8 mm" in outlook.summary


async def test_past_day_uses_climatology_and_skips_incomplete_year(respx_mock):
    archive = archive_route(respx_mock)
    # Last week relative to "today": the current year's window is not yet in the archive.
    outlook = await OpenMeteoWeatherProvider(today=today, climatology_years=3).get_outlook(AUSTIN, date(2026, 11, 7))

    assert outlook is not None and outlook.basis == "climatology"
    years = sorted(c.request.url.params["start_date"][:4] for c in archive.calls)
    assert years == ["2023", "2024", "2025"]


async def test_climatology_window_summary_uses_hourly_archive(respx_mock):
    archive = archive_route(respx_mock, wet_days=3, hourly=True)

    outlook = await OpenMeteoWeatherProvider(today=today).get_outlook(
        AUSTIN, date(2027, 6, 12), start_hour=19, end_hour=23
    )

    assert all("hourly" in c.request.url.params for c in archive.calls)
    assert outlook is not None and outlook.window_summary is not None
    assert outlook.window_summary.startswith("19:00–23:00 at this time of year: typically 20–21°C")
    assert "on about 43% of days" in outlook.window_summary  # 3 of 7 days have rain at 20:00


async def test_leap_day_maps_to_28_february(respx_mock):
    archive = archive_route(respx_mock)

    outlook = await OpenMeteoWeatherProvider(today=today, climatology_years=4).get_outlook(AUSTIN, date(2028, 2, 29))

    assert outlook is not None
    centres = sorted(c.request.url.params["end_date"] for c in archive.calls)
    assert centres == ["2023-03-03", "2024-03-03", "2025-03-03", "2026-03-03"]


# --- failures ------------------------------------------------------------------------------------


async def test_forecast_error_falls_back_to_climatology(respx_mock):
    respx_mock.get(FORECAST_URL).mock(
        return_value=httpx.Response(400, json={"error": True, "reason": "start_date out of range"})
    )
    archive_route(respx_mock)

    outlook = await OpenMeteoWeatherProvider(today=today).get_outlook(AUSTIN, TODAY + timedelta(days=15))

    assert outlook is not None and outlook.basis == "climatology"


async def test_upstream_failure_returns_none(respx_mock):
    respx_mock.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("down"))
    respx_mock.get(ARCHIVE_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    assert await OpenMeteoWeatherProvider(today=today).get_outlook(AUSTIN, TODAY + timedelta(days=2)) is None


async def test_too_few_archive_years_returns_none(respx_mock):
    calls = iter(range(100))

    def flaky(request: httpx.Request) -> httpx.Response:
        if next(calls) < 8:
            return httpx.Response(429, json={"error": True, "reason": "rate limited"})
        start = date.fromisoformat(request.url.params["start_date"])
        return httpx.Response(200, json=archive_payload(start))

    respx_mock.get(ARCHIVE_URL).mock(side_effect=flaky)

    assert await OpenMeteoWeatherProvider(today=today).get_outlook(AUSTIN, date(2027, 6, 12)) is None


async def test_malformed_payload_is_treated_as_failure(respx_mock):
    respx_mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json={"daily": "nonsense"}))
    respx_mock.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, text="not json"))

    assert await OpenMeteoWeatherProvider(today=today).get_outlook(AUSTIN, TODAY) is None


# --- mock provider and factory -------------------------------------------------------------------


async def test_mock_is_deterministic_and_seasonal():
    mock = MockWeatherProvider(today=today)
    north_summer = await mock.get_outlook(GeoPoint(lat=48.9, lng=2.3), date(2027, 7, 15))
    north_winter = await mock.get_outlook(GeoPoint(lat=48.9, lng=2.3), date(2027, 1, 15))
    south_january = await mock.get_outlook(GeoPoint(lat=-33.9, lng=151.2), date(2027, 1, 15))
    again = await mock.get_outlook(GeoPoint(lat=48.9, lng=2.3), date(2027, 7, 15))

    assert north_summer == again
    assert north_summer.temp_high_c > north_winter.temp_high_c
    assert south_january.temp_high_c > north_winter.temp_high_c
    assert north_summer.basis == "climatology" and "not a forecast" in north_summer.summary
    assert 0 <= north_winter.precipitation_probability <= 1


async def test_mock_forecast_window_and_horizon():
    mock = MockWeatherProvider(today=today)
    soon = await mock.get_outlook(AUSTIN, TODAY + timedelta(days=2), start_hour=18, end_hour=22)

    assert soon.basis == "forecast" and soon.summary.startswith("Forecast for")
    assert soon.window_summary.startswith("18:00–22:00:")
    assert soon.temp_low_c <= soon.temp_high_c


def test_build_weather_provider(settings):
    assert isinstance(build_weather_provider(settings), MockWeatherProvider)
    assert isinstance(
        build_weather_provider(settings.model_copy(update={"weather_provider": "auto"})), OpenMeteoWeatherProvider
    )
    assert build_weather_provider(settings.model_copy(update={"weather_provider": "open_meteo"})).name == "open_meteo"
