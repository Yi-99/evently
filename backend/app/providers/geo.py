"""Geographic helpers shared by the weather and traffic providers."""

from __future__ import annotations

import logging
import math
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.providers.base import GeoPoint

log = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points, in kilometres."""
    lat_a, lat_b = math.radians(a.lat), math.radians(b.lat)
    d_lat = lat_b - lat_a
    d_lng = math.radians(b.lng - a.lng)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(d_lng / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def longitude_timezone(lng: float) -> tzinfo:
    """Fixed-offset zone from longitude alone (15 degrees per hour).

    Ignores political boundaries and daylight saving, so it can be an hour or two off;
    use it only when a real lookup is unavailable.
    """
    return timezone(timedelta(hours=round(lng / 15)))


class TimezoneResolver:
    """Timezone of a coordinate, looked up from Open-Meteo (no key needed) and cached.

    `resolve` never raises: when the lookup fails, or `online` is False, it returns
    `longitude_timezone`. Failed lookups are not cached, so a later call retries.
    """

    def __init__(self, *, online: bool = True, timeout: float = 5.0) -> None:
        self._online = online
        self._timeout = timeout
        # Keyed by coordinates rounded to ~1 km; an app only ever sees a handful of venue areas.
        self._cache: dict[tuple[float, float], tzinfo] = {}

    async def resolve(self, point: GeoPoint) -> tzinfo:
        if not self._online:
            return longitude_timezone(point.lng)
        key = (round(point.lat, 2), round(point.lng, 2))
        if key not in self._cache:
            zone = await self._lookup(point)
            if zone is None:
                return longitude_timezone(point.lng)
            self._cache[key] = zone
        return self._cache[key]

    async def _lookup(self, point: GeoPoint) -> tzinfo | None:
        # A forecast request with no variables returns only the location metadata, including its zone.
        params = {"latitude": point.lat, "longitude": point.lng, "timezone": "auto", "forecast_days": 1}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(OPEN_METEO_FORECAST_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            return _zone_from_payload(payload)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            log.warning("timezone lookup failed for %.2f,%.2f: %s", point.lat, point.lng, exc)
            return None


def _zone_from_payload(payload: dict) -> tzinfo | None:
    """Prefer the IANA zone (handles DST on the event date); fall back to the current UTC offset."""
    name = payload.get("timezone")
    if isinstance(name, str):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass  # no tz database on this host, or an unknown name
    offset = payload.get("utc_offset_seconds")
    if isinstance(offset, int | float):
        return timezone(timedelta(seconds=offset))
    return None
