"""Traffic provider (typical vs. event-time drive times, rush-hour overlap).

`GoogleRoutesTrafficProvider` asks the Routes API for a traffic-aware drive time at the event's
arrival time; `HeuristicTrafficProvider` estimates from great-circle distance and weekday peak
patterns and needs no key or network. Both label their `basis`, and the Google provider hands
over to the heuristic (still labelled "heuristic") when the live call fails.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, NamedTuple

import httpx

from app.core.config import Settings
from app.providers.base import GeoPoint, ProviderError, TrafficProvider, TravelEstimate
from app.providers.geo import TimezoneResolver, haversine_km

log = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTES_FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"

# Weekday peaks (venue-local wall clock) used by both providers for `rush_hour_overlap`.
MORNING_PEAK = (time(7, 0), time(9, 30))
EVENING_PEAK = (time(16, 0), time(19, 0))
ASSUMED_DRIVE_MINUTES = 30.0  # when the drive length is unknown, judge peak overlap on this much

# Heuristic travel-time model.
ROAD_WINDING_FACTOR = 1.3  # road distance / great-circle distance
# (road km, average km/h) anchors, linearly interpolated: short urban hops are slow, long trips mostly highway.
_SPEED_ANCHORS = [(0.0, 20.0), (5.0, 30.0), (20.0, 45.0), (60.0, 70.0), (150.0, 90.0)]
# Traffic multipliers on top of the free-flow time, by local time of the drive's midpoint.
_WEEKDAY_BANDS = [
    (time(6, 0), time(7, 0), 1.2),
    (MORNING_PEAK[0], MORNING_PEAK[1], 1.45),
    (time(9, 30), time(10, 30), 1.2),
    (time(10, 30), time(15, 0), 1.1),
    (time(15, 0), time(16, 0), 1.25),
    (EVENING_PEAK[0], EVENING_PEAK[1], 1.55),
    (time(19, 0), time(20, 0), 1.2),
]
_WEEKEND_BANDS = [(time(10, 0), time(19, 0), 1.1)]

_DEPARTURE_MARGIN = timedelta(minutes=5)  # Google rejects departure times that have already passed
_LATLNG_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def build_traffic_provider(settings: Settings) -> TrafficProvider:
    """Pick from `settings.traffic_provider` ("auto" = Google Routes when a Maps key is set, else heuristic)."""
    # Tests must never touch the network, so under test the venue timezone comes from longitude alone.
    timezones = TimezoneResolver(online=settings.environment != "test")
    heuristic = HeuristicTrafficProvider(timezones=timezones)
    if settings.traffic_provider == "heuristic":
        return heuristic
    if settings.maps_api_key:
        return GoogleRoutesTrafficProvider(settings.maps_api_key, timezones=timezones, fallback=heuristic)
    if settings.traffic_provider == "google":
        raise ProviderError("traffic_provider=google needs GOOGLE_MAPS_API_KEY (or GOOGLE_API_KEY)")
    return heuristic


# --- shared time helpers ---------------------------------------------------------------------


def venue_local(arrive_by: datetime, zone: tzinfo) -> datetime:
    """Interpret a naive `arrive_by` as venue wall-clock time; convert an aware one into the venue zone."""
    if arrive_by.tzinfo is None:
        return arrive_by.replace(tzinfo=zone)
    return arrive_by.astimezone(zone)


def overlaps_weekday_peak(arrival: datetime, drive_minutes: float) -> bool:
    """Does a drive of `drive_minutes` ending at `arrival` (venue-local) touch a weekday peak?"""
    arrival = arrival.replace(tzinfo=None)  # wall-clock comparison, immune to DST arithmetic
    departure = arrival - timedelta(minutes=drive_minutes)
    day = departure.date()
    while day <= arrival.date():
        if day.weekday() < 5:
            for start, end in (MORNING_PEAK, EVENING_PEAK):
                if departure < datetime.combine(day, end) and arrival > datetime.combine(day, start):
                    return True
        day += timedelta(days=1)
    return False


def traffic_multiplier(at: datetime) -> float:
    """Heuristic slowdown factor for a drive whose midpoint is at venue-local `at`."""
    bands = _WEEKDAY_BANDS if at.weekday() < 5 else _WEEKEND_BANDS
    moment = at.time().replace(tzinfo=None)
    for start, end, factor in bands:
        if start <= moment < end:
            return factor
    return 1.0


def _origin_label(origin: GeoPoint | str) -> str:
    return origin if isinstance(origin, str) else f"{origin.lat:.5f},{origin.lng:.5f}"


def _parse_latlng(text: str) -> GeoPoint | None:
    match = _LATLNG_RE.match(text)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    return GeoPoint(lat=lat, lng=lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def _clock_label(at: datetime) -> str:
    return f"{at:%a} {at.day} {at:%b} {at:%H:%M}"


# --- heuristic -------------------------------------------------------------------------------


def heuristic_free_flow_minutes(road_km: float) -> float:
    """Free-flow drive time for `road_km` using the interpolated speed anchors."""
    anchors = _SPEED_ANCHORS
    if road_km >= anchors[-1][0]:
        speed = anchors[-1][1]
    else:
        speed = anchors[0][1]
        for (d0, v0), (d1, v1) in zip(anchors, anchors[1:], strict=False):
            if d0 <= road_km <= d1:
                speed = v0 + (v1 - v0) * (road_km - d0) / (d1 - d0)
                break
    return 60 * road_km / speed


class HeuristicTrafficProvider:
    """Distance-and-clock estimate; never calls Google and never returns None.

    `origin` strings are understood only as "lat,lng"; for anything else the minutes are left None
    and only `rush_hour_overlap` is judged, from the arrival time.
    """

    name = "heuristic"

    def __init__(self, *, timezones: TimezoneResolver | None = None) -> None:
        self._timezones = timezones or TimezoneResolver(online=False)

    async def estimate_travel(
        self, origin: GeoPoint | str, destination: GeoPoint, *, arrive_by: datetime | None = None
    ) -> TravelEstimate | None:
        arrival = None
        if arrive_by is not None:
            # A naive arrive_by already is venue wall-clock time; only aware ones need the venue zone.
            zone = arrive_by.tzinfo if arrive_by.tzinfo is None else await self._timezones.resolve(destination)
            arrival = venue_local(arrive_by, zone)  # type: ignore[arg-type]
        label = _origin_label(origin)
        start = origin if isinstance(origin, GeoPoint) else _parse_latlng(origin)
        if start is None:
            return TravelEstimate(
                origin=label,
                rush_hour_overlap=arrival is not None and overlaps_weekday_peak(arrival, ASSUMED_DRIVE_MINUTES),
                basis="heuristic",
                notes=(
                    f"Could not locate origin '{label}' without a maps key, so no drive time is estimated; "
                    "rush-hour overlap is judged from the arrival time alone."
                ),
            )
        road_km = haversine_km(start, destination) * ROAD_WINDING_FACTOR
        typical = heuristic_free_flow_minutes(road_km)
        if arrival is None:
            return TravelEstimate(
                origin=label,
                typical_minutes=round(typical, 1),
                distance_km=round(road_km, 1),
                basis="heuristic",
                notes="Rough estimate from straight-line distance; no arrival time given, so no traffic adjustment.",
            )
        factor = traffic_multiplier(arrival - timedelta(minutes=typical / 2))
        return TravelEstimate(
            origin=label,
            typical_minutes=round(typical, 1),
            event_time_minutes=round(typical * factor, 1),
            distance_km=round(road_km, 1),
            delay_ratio=round(factor, 2),
            rush_hour_overlap=overlaps_weekday_peak(arrival, typical),
            basis="heuristic",
            notes=(
                f"Rough estimate from straight-line distance and typical weekday traffic patterns for arriving "
                f"{_clock_label(arrival)} (venue time); not live traffic."
            ),
        )


# --- Google Routes API -----------------------------------------------------------------------


class _Route(NamedTuple):
    duration_s: float
    static_s: float
    distance_m: int | None


class GoogleRoutesTrafficProvider:
    """Drive times from the Routes API `computeRoutes` (traffic-aware, tight field mask).

    The Routes API only predicts traffic for a *departure* time (arrival times are transit-only),
    so a first traffic-unaware call gives the static duration and the departure is derived as
    arrival minus that. Departure times must be in the future: past or imminent arrivals are
    moved to the next same weekday and time, and the notes say so.
    """

    name = "google_routes"

    def __init__(
        self,
        api_key: str,
        *,
        timezones: TimezoneResolver | None = None,
        fallback: TrafficProvider | None = None,
        now: Callable[[], datetime] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._timezones = timezones or TimezoneResolver()
        self._fallback = fallback
        self._now = now or (lambda: datetime.now(UTC))
        self._timeout = timeout

    async def estimate_travel(
        self, origin: GeoPoint | str, destination: GeoPoint, *, arrive_by: datetime | None = None
    ) -> TravelEstimate | None:
        try:
            return await self._routes_estimate(origin, destination, arrive_by)
        except ProviderError as exc:
            log.warning("Google Routes unavailable for %s: %s", _origin_label(origin), exc)
            if self._fallback is None:
                return None
            estimate = await self._fallback.estimate_travel(origin, destination, arrive_by=arrive_by)
            if estimate is None:
                return None
            note = "Live traffic was unavailable (Google Routes request failed), so this is a rough estimate."
            return estimate.model_copy(update={"notes": f"{note} {estimate.notes}" if estimate.notes else note})

    async def _routes_estimate(
        self, origin: GeoPoint | str, destination: GeoPoint, arrive_by: datetime | None
    ) -> TravelEstimate:
        label = _origin_label(origin)
        arrival = venue_local(arrive_by, await self._timezones.resolve(destination)) if arrive_by else None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            static = await self._compute_route(client, origin, destination)
            if static is None:
                return TravelEstimate(
                    origin=label,
                    rush_hour_overlap=arrival is not None and overlaps_weekday_peak(arrival, ASSUMED_DRIVE_MINUTES),
                    basis="routes_api",
                    notes="Google found no drivable route from this origin to the venue.",
                )
            if arrival is None:
                return TravelEstimate(
                    origin=label,
                    typical_minutes=round(static.static_s / 60, 1),
                    distance_km=_km(static.distance_m),
                    basis="routes_api",
                    notes="No arrival time given, so only the typical (traffic-free) drive time was computed.",
                )
            static_drive = timedelta(seconds=static.static_s)
            predicted_arrival, shifted = _future_arrival(arrival, static_drive, self._now())
            route = await self._compute_route(client, origin, destination, departure=predicted_arrival - static_drive)
        if route is None:
            raise ProviderError("traffic-aware computeRoutes returned no route")
        typical = route.static_s / 60
        event_time = route.duration_s / 60
        notes = (
            f"Google Routes traffic prediction for departing {_clock_label(predicted_arrival - static_drive)} "
            f"to arrive {_clock_label(predicted_arrival)} (venue time)."
        )
        if shifted:
            notes += (
                f" The requested arrival ({_clock_label(arrival)}) is in the past, so traffic was predicted for "
                f"the next {predicted_arrival:%A} at {predicted_arrival:%H:%M}."
            )
        return TravelEstimate(
            origin=label,
            typical_minutes=round(typical, 1),
            event_time_minutes=round(event_time, 1),
            distance_km=_km(route.distance_m),
            delay_ratio=round(event_time / typical, 2) if typical > 0 else None,
            rush_hour_overlap=overlaps_weekday_peak(predicted_arrival, event_time),
            basis="routes_api",
            notes=notes,
        )

    async def _compute_route(
        self,
        client: httpx.AsyncClient,
        origin: GeoPoint | str,
        destination: GeoPoint,
        *,
        departure: datetime | None = None,
    ) -> _Route | None:
        """One `computeRoutes` call; traffic-aware when `departure` is given. None when no route exists."""
        body: dict[str, Any] = {
            "origin": _waypoint(origin),
            "destination": _waypoint(destination),
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE" if departure is None else "TRAFFIC_AWARE_OPTIMAL",
        }
        if departure is not None:
            body["departureTime"] = departure.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": ROUTES_FIELD_MASK}
        try:
            response = await client.post(ROUTES_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"computeRoutes request failed: {exc}") from exc
        if response.status_code != 200:
            raise ProviderError(f"computeRoutes returned {response.status_code}: {_error_message(response)}")
        try:
            routes = response.json().get("routes") or []
        except ValueError as exc:
            raise ProviderError("computeRoutes returned invalid JSON") from exc
        if not routes:
            return None
        route = routes[0]
        duration = _seconds(route.get("duration"))
        if duration is None:
            raise ProviderError("computeRoutes route has no duration")
        static = _seconds(route.get("staticDuration"))
        return _Route(duration, static if static is not None else duration, route.get("distanceMeters"))


def _future_arrival(arrival: datetime, drive: timedelta, now: datetime) -> tuple[datetime, bool]:
    """Move `arrival` forward by whole weeks until its departure is still ahead of `now`."""
    earliest = now + _DEPARTURE_MARGIN + drive
    if arrival >= earliest:
        return arrival, False
    weeks = math.ceil((earliest - arrival) / timedelta(weeks=1))
    shifted = arrival + timedelta(weeks=weeks)  # wall-clock arithmetic keeps the local time across DST
    if shifted < earliest:
        shifted += timedelta(weeks=1)
    return shifted, True


def _waypoint(place: GeoPoint | str) -> dict[str, Any]:
    if isinstance(place, str):
        return {"address": place}
    return {"location": {"latLng": {"latitude": place.lat, "longitude": place.lng}}}


def _seconds(text: Any) -> float | None:
    """Routes API durations are strings like "165s"."""
    if isinstance(text, str) and text.endswith("s"):
        try:
            return float(text[:-1])
        except ValueError:
            return None
    return None


def _km(meters: Any) -> float | None:
    return round(meters / 1000, 1) if isinstance(meters, int | float) else None


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        return f"{error.get('status', '')} {error.get('message', '')}".strip() or response.text[:200]
    except (ValueError, AttributeError):
        return response.text[:200]


__all__ = [
    "GoogleRoutesTrafficProvider",
    "HeuristicTrafficProvider",
    "build_traffic_provider",
    "heuristic_free_flow_minutes",
    "overlaps_weekday_peak",
    "traffic_multiplier",
    "venue_local",
]
