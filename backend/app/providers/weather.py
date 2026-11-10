"""Weather provider (forecast when in range, climatology otherwise).

Open-Meteo needs no API key. Days within its 16-day horizon get a real forecast; earlier or later
days get a *climatology*: the same calendar window (±3 days) over the last several years from the
historical archive, so a planner booking months ahead still sees typical conditions, clearly
labelled as such.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings
from app.providers.base import GeoPoint, WeatherOutlook, WeatherProvider

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_HORIZON_DAYS = 16  # Open-Meteo forecasts today plus the next 15 days
CLIMATOLOGY_WINDOW_DAYS = 3  # ±3 days around the same calendar date
ARCHIVE_DELAY_DAYS = 7  # ERA5 reanalysis lags real time by ~5 days
WET_MM = 1.0  # precipitation at or above this counts as a wet day / wet window

_DAILY_FORECAST_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
    "precipitation_sum,wind_speed_10m_max"
)
_HOURLY_FORECAST_VARS = "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m"
_DAILY_ARCHIVE_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
_HOURLY_ARCHIVE_VARS = "temperature_2m,precipitation"
_MIN_CLIMATOLOGY_YEARS = 3

# WMO 4677 weather interpretation codes, as Open-Meteo documents them.
WMO_WEATHER_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    97: "heavy thunderstorm",
    99: "thunderstorm with heavy hail",
}


def describe_weather_code(code: int | float | None) -> str | None:
    if code is None:
        return None
    return WMO_WEATHER_CODES.get(int(code), f"weather code {int(code)}")


def build_weather_provider(settings: Settings) -> WeatherProvider:
    """Pick the provider from `settings.weather_provider` ("auto" = Open-Meteo, which needs no key)."""
    if settings.weather_provider == "mock":
        return MockWeatherProvider()
    return OpenMeteoWeatherProvider()


# --- shared helpers ---------------------------------------------------------------------------


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _window_hours(start_hour: int | None, end_hour: int | None) -> range | None:
    """Local hours [start, end) the caller asked about, or None when no window was given.

    A window that runs past midnight is truncated at the end of `day`.
    """
    if start_hour is None and end_hour is None:
        return None
    start = min(max(start_hour or 0, 0), 23)
    end = 24 if end_hour is None else min(max(end_hour, 0), 24)
    if end <= start:
        end = 24
    return range(start, end)


def _window_label(window: range) -> str:
    return f"{window.start:02d}:00–{window.stop:02d}:00"


def _day_label(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b %Y}"


def _season_label(day: date) -> str:
    part = "early" if day.day <= 10 else "mid" if day.day <= 20 else "late"
    return f"{part}-{day:%B}" if part == "mid" else f"{part} {day:%B}"


def _temp_range(low: float | None, high: float | None) -> str | None:
    if low is None or high is None:
        return None
    lo, hi = round(low), round(high)
    return f"{lo}°C" if lo == hi else f"{lo}–{hi}°C"


def _join(parts: list[str | None]) -> str:
    return ", ".join(p for p in parts if p)


def _forecast_summary(
    day: date,
    description: str | None,
    high: float | None,
    low: float | None,
    precip_probability: float | None,
    precip_mm: float | None,
    wind_kph: float | None,
) -> str:
    rain = None
    if precip_probability is not None:
        rain = f"{round(precip_probability * 100)}% chance of precipitation"
        if precip_mm:
            rain += f" (about {precip_mm:.0f} mm)"
    details = _join(
        [
            description,
            f"high {high:.0f}°C" if high is not None else None,
            f"low {low:.0f}°C" if low is not None else None,
            rain,
            f"wind up to {wind_kph:.0f} km/h" if wind_kph is not None else None,
        ]
    )
    return f"Forecast for {_day_label(day)}: {details}."


def _climatology_summary(
    day: date,
    source: str,
    high: float | None,
    low: float | None,
    wet_share: float | None,
    wet_day_mm: float | None,
    wind_kph: float | None,
) -> str:
    rain = None
    if wet_share is not None:
        rain = f"measurable precipitation on about {round(wet_share * 100)}% of days"
        if wet_day_mm:
            rain += f" (wet days average {wet_day_mm:.0f} mm)"
    details = _join(
        [
            f"highs around {high:.0f}°C" if high is not None else None,
            f"lows around {low:.0f}°C" if low is not None else None,
            rain,
            f"winds typically up to {wind_kph:.0f} km/h" if wind_kph is not None else None,
        ]
    )
    return (
        f"Typical conditions for {_season_label(day)}, from {source} "
        f"(a climatology estimate of usual weather, not a forecast): {details}."
    )


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


# --- Open-Meteo ------------------------------------------------------------------------------


class OpenMeteoWeatherProvider:
    """Forecast from api.open-meteo.com; climatology from archive-api.open-meteo.com.

    `today` is injectable so tests can pin which branch a date falls in.
    Returns None only when the needed upstream data cannot be fetched at all.
    """

    name = "open_meteo"

    def __init__(
        self, *, today: Callable[[], date] | None = None, climatology_years: int = 10, timeout: float = 10.0
    ) -> None:
        self._today = today or _utc_today
        self._years = climatology_years
        self._timeout = timeout

    async def get_outlook(
        self, location: GeoPoint, day: date, *, start_hour: int | None = None, end_hour: int | None = None
    ) -> WeatherOutlook | None:
        window = _window_hours(start_hour, end_hour)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if 0 <= (day - self._today()).days < FORECAST_HORIZON_DAYS:
                outlook = await self._forecast(client, location, day, window)
                # The last horizon days are sometimes empty (model coverage, timezone edge): fall through.
                if outlook is not None:
                    return outlook
            return await self._climatology(client, location, day, window)

    async def _forecast(
        self, client: httpx.AsyncClient, location: GeoPoint, day: date, window: range | None
    ) -> WeatherOutlook | None:
        params: dict[str, Any] = {
            "latitude": location.lat,
            "longitude": location.lng,
            "daily": _DAILY_FORECAST_VARS,
            "timezone": "auto",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        }
        if window is not None:
            params["hourly"] = _HOURLY_FORECAST_VARS
        payload = await _get_json(client, FORECAST_URL, params)
        if payload is None:
            return None
        try:
            return _forecast_outlook(payload, day, window)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            log.warning("unexpected Open-Meteo forecast payload: %s", exc)
            return None

    async def _climatology(
        self, client: httpx.AsyncClient, location: GeoPoint, day: date, window: range | None
    ) -> WeatherOutlook | None:
        windows = _climatology_windows(day, self._today(), self._years)
        params = [
            {
                "latitude": location.lat,
                "longitude": location.lng,
                "daily": _DAILY_ARCHIVE_VARS,
                "timezone": "auto",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                **({"hourly": _HOURLY_ARCHIVE_VARS} if window is not None else {}),
            }
            for start, end in windows
        ]
        payloads = await asyncio.gather(*(_get_json(client, ARCHIVE_URL, p) for p in params))
        years = [start.year for (start, _), payload in zip(windows, payloads, strict=True) if payload is not None]
        if len(years) < _MIN_CLIMATOLOGY_YEARS:
            log.warning("climatology for %s: only %d of %d archive years available", day, len(years), len(windows))
            return None
        try:
            return _climatology_outlook([p for p in payloads if p is not None], day, window, years)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            log.warning("unexpected Open-Meteo archive payload: %s", exc)
            return None


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET and decode, logging and returning None on any transport, HTTP or decoding failure."""
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        reason = exc.response.text[:200]
        log.warning("Open-Meteo %s returned %s: %s", url, exc.response.status_code, reason)
        return None
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Open-Meteo %s failed: %s", url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _series(block: dict[str, Any] | None, key: str) -> list[Any]:
    values = (block or {}).get(key)
    return values if isinstance(values, list) else []


def _first(block: dict[str, Any] | None, key: str) -> float | None:
    values = _series(block, key)
    return values[0] if values and values[0] is not None else None


def _forecast_outlook(payload: dict[str, Any], day: date, window: range | None) -> WeatherOutlook | None:
    daily = payload.get("daily") or {}
    if _series(daily, "time")[:1] != [day.isoformat()]:
        return None
    high, low = _first(daily, "temperature_2m_max"), _first(daily, "temperature_2m_min")
    if high is None and low is None:
        return None  # the day is nominally in range but the model has nothing for it
    probability_pct = _first(daily, "precipitation_probability_max")
    probability = None if probability_pct is None else probability_pct / 100
    precip_mm = _first(daily, "precipitation_sum")
    wind = _first(daily, "wind_speed_10m_max")
    description = describe_weather_code(_first(daily, "weather_code"))
    return WeatherOutlook(
        day=day,
        basis="forecast",
        summary=_forecast_summary(day, description, high, low, probability, precip_mm, wind),
        temp_high_c=_round(high),
        temp_low_c=_round(low),
        precipitation_probability=_round(probability, 2),
        precipitation_mm=_round(precip_mm),
        wind_kph=_round(wind),
        window_summary=_forecast_window_summary(payload.get("hourly"), day, window) if window else None,
    )


def _window_rows(hourly: dict[str, Any] | None, day: date | None, window: range) -> list[int]:
    """Indices of hourly rows that fall inside `window` (on `day`, or on any day when None)."""
    rows = []
    for index, stamp in enumerate(_series(hourly, "time")):
        at = datetime.fromisoformat(stamp)
        if at.hour in window and (day is None or at.date() == day):
            rows.append(index)
    return rows


def _pick(hourly: dict[str, Any] | None, key: str, rows: list[int]) -> list[float]:
    values = _series(hourly, key)
    return [values[i] for i in rows if i < len(values) and values[i] is not None]


def _forecast_window_summary(hourly: dict[str, Any] | None, day: date, window: range) -> str | None:
    rows = _window_rows(hourly, day, window)
    if not rows:
        return None
    temps = _pick(hourly, "temperature_2m", rows)
    probabilities = _pick(hourly, "precipitation_probability", rows)
    winds = _pick(hourly, "wind_speed_10m", rows)
    codes = [int(c) for c in _pick(hourly, "weather_code", rows)]
    description = None
    if codes:
        usual = max(set(codes), key=lambda c: (codes.count(c), c))
        description = describe_weather_code(usual)
        worst = max(codes)
        # Codes rise roughly with severity; call out precipitation that is not the dominant condition.
        if worst >= 51 and worst != usual:
            description += f", {describe_weather_code(worst)} at times"
    details = _join(
        [
            description,
            _temp_range(min(temps), max(temps)) if temps else None,
            f"up to {max(probabilities):.0f}% chance of precipitation" if probabilities else None,
            f"wind up to {max(winds):.0f} km/h" if winds else None,
        ]
    )
    return f"{_window_label(window)}: {details}." if details else None


def _same_calendar_day(day: date, year: int) -> date:
    try:
        return day.replace(year=year)
    except ValueError:  # 29 February in a non-leap year
        return date(year, 2, 28)


def _climatology_windows(day: date, today: date, years: int) -> list[tuple[date, date]]:
    """The `years` most recent complete ±N-day windows around `day`'s calendar date, newest first."""
    windows: list[tuple[date, date]] = []
    year = today.year
    while len(windows) < years and year > 1940:
        centre = _same_calendar_day(day, year)
        start = centre - timedelta(days=CLIMATOLOGY_WINDOW_DAYS)
        end = centre + timedelta(days=CLIMATOLOGY_WINDOW_DAYS)
        if end <= today - timedelta(days=ARCHIVE_DELAY_DAYS):
            windows.append((start, end))
        year -= 1
    return windows


def _climatology_outlook(
    payloads: list[dict[str, Any]], day: date, window: range | None, years: list[int]
) -> WeatherOutlook | None:
    highs: list[float] = []
    lows: list[float] = []
    precip: list[float] = []
    winds: list[float] = []
    for payload in payloads:
        daily = payload.get("daily") or {}
        highs += [v for v in _series(daily, "temperature_2m_max") if v is not None]
        lows += [v for v in _series(daily, "temperature_2m_min") if v is not None]
        precip += [v for v in _series(daily, "precipitation_sum") if v is not None]
        winds += [v for v in _series(daily, "wind_speed_10m_max") if v is not None]
    if not highs and not lows:
        return None
    wet_days = [p for p in precip if p >= WET_MM]
    wet_share = len(wet_days) / len(precip) if precip else None
    span = f"{min(years)}–{max(years)}" if min(years) != max(years) else str(years[0])
    source = f"{span} records"
    return WeatherOutlook(
        day=day,
        basis="climatology",
        summary=_climatology_summary(day, source, _mean(highs), _mean(lows), wet_share, _mean(wet_days), _mean(winds)),
        temp_high_c=_round(_mean(highs)),
        temp_low_c=_round(_mean(lows)),
        precipitation_probability=_round(wet_share, 2),
        precipitation_mm=_round(_mean(precip)),
        wind_kph=_round(_mean(winds)),
        window_summary=_climatology_window_summary(payloads, window) if window else None,
    )


def _climatology_window_summary(payloads: list[dict[str, Any]], window: range) -> str | None:
    """Typical temperatures in the window and how often it rains during those hours."""
    day_lows: list[float] = []
    day_highs: list[float] = []
    wet_windows = 0
    days = 0
    for payload in payloads:
        hourly = payload.get("hourly") or {}
        by_day: dict[date, list[int]] = {}
        for index in _window_rows(hourly, None, window):
            by_day.setdefault(datetime.fromisoformat(hourly["time"][index]).date(), []).append(index)
        for rows in by_day.values():
            temps = _pick(hourly, "temperature_2m", rows)
            if temps:
                day_lows.append(min(temps))
                day_highs.append(max(temps))
            rain = _pick(hourly, "precipitation", rows)
            if rain:
                days += 1
                wet_windows += sum(rain) >= WET_MM
    if not day_highs:
        return None
    text = f"{_window_label(window)} at this time of year: typically {_temp_range(_mean(day_lows), _mean(day_highs))}"
    if days:
        text += f", with rain during those hours on about {round(100 * wet_windows / days)}% of days"
    return text + "."


# --- mock ------------------------------------------------------------------------------------


class MockWeatherProvider:
    """Deterministic, plausible weather from latitude and month only; no network.

    Uses the same forecast/climatology horizon rule as Open-Meteo so callers exercise both labels.
    """

    name = "mock"

    def __init__(self, *, today: Callable[[], date] | None = None) -> None:
        self._today = today or _utc_today

    async def get_outlook(
        self, location: GeoPoint, day: date, *, start_hour: int | None = None, end_hour: int | None = None
    ) -> WeatherOutlook | None:
        window = _window_hours(start_hour, end_hour)
        in_horizon = 0 <= (day - self._today()).days < FORECAST_HORIZON_DAYS
        abs_lat = abs(location.lat)
        # Warm-season factor: +1 at the summer peak, -1 at the winter trough for this hemisphere.
        peak_month = 7 if location.lat >= 0 else 1
        season = math.cos(2 * math.pi * (day.month - peak_month) / 12)
        mean_temp = 27 - 0.45 * max(0.0, abs_lat - 15) + min(14.0, 0.22 * abs_lat) * season
        high, low = mean_temp + 5, mean_temp - 5
        tropical = abs_lat < 23.5
        probability = min(0.9, max(0.05, 0.35 + 0.2 * season if tropical else 0.3 - 0.1 * season))
        precip_mm = probability * 8
        wind = 12 + 0.1 * abs_lat - 2 * season
        description = _mock_description(probability, mean_temp)
        if in_horizon:
            summary = _forecast_summary(day, description, high, low, probability, precip_mm, wind)
        else:
            wet_day_mm = precip_mm / probability
            summary = _climatology_summary(day, "typical patterns", high, low, probability, wet_day_mm, wind)
        return WeatherOutlook(
            day=day,
            basis="forecast" if in_horizon else "climatology",
            summary=f"{summary} (mock data)",
            temp_high_c=round(high, 1),
            temp_low_c=round(low, 1),
            precipitation_probability=round(probability, 2),
            precipitation_mm=round(precip_mm, 1),
            wind_kph=round(wind, 1),
            window_summary=_mock_window_summary(window, high, low, description) if window else None,
        )


def _mock_description(probability: float, mean_temp: float) -> str:
    if probability >= 0.55:
        return "snow showers" if mean_temp < 1 else "rain showers"
    if probability >= 0.4:
        return "overcast"
    return "partly cloudy" if probability >= 0.2 else "mainly clear"


def _mock_window_summary(window: range, high: float, low: float, description: str) -> str:
    # Simple diurnal curve: warmest at 15:00, coldest at 03:00.
    temps = [low + (high - low) * (0.5 + 0.5 * math.cos(2 * math.pi * (h - 15) / 24)) for h in window]
    return f"{_window_label(window)}: {description}, {_temp_range(min(temps), max(temps))}. (mock data)"
