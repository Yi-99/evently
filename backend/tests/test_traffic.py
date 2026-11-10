"""Traffic providers: Google Routes request/response, departure derivation, fallbacks, heuristic, timezones."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.providers.base import GeoPoint, ProviderError
from app.providers.geo import OPEN_METEO_FORECAST_URL, TimezoneResolver, haversine_km, longitude_timezone
from app.providers.traffic import (
    ROUTES_URL,
    GoogleRoutesTrafficProvider,
    HeuristicTrafficProvider,
    build_traffic_provider,
    heuristic_free_flow_minutes,
    overlaps_weekday_peak,
    traffic_multiplier,
)

CHICAGO = ZoneInfo("America/Chicago")
VENUE = GeoPoint(lat=30.27, lng=-97.74)  # downtown Austin
ORIGIN = GeoPoint(lat=30.40, lng=-97.70)  # ~15 km north
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)  # a Monday
THURSDAY_18 = datetime(2026, 11, 12, 18, 0)  # naive = venue-local
SATURDAY_18 = datetime(2026, 11, 14, 18, 0)


class FixedZone:
    """Stand-in for TimezoneResolver that never touches the network."""

    def __init__(self, zone=CHICAGO):
        self.zone = zone
        self.calls = 0

    async def resolve(self, point):
        self.calls += 1
        return self.zone


def now() -> datetime:
    return NOW


def route_response(duration_s: int, static_s: int, distance_m: int = 18200) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "routes": [{"duration": f"{duration_s}s", "staticDuration": f"{static_s}s", "distanceMeters": distance_m}]
        },
    )


def google(respx_mock, static_s=1200, traffic_s=1800):
    """Mock computeRoutes: traffic-unaware calls get `static_s`, traffic-aware ones `traffic_s`."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["routingPreference"] == "TRAFFIC_UNAWARE":
            return route_response(static_s, static_s)
        return route_response(traffic_s, static_s)

    return respx_mock.post(ROUTES_URL).mock(side_effect=respond)


def bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


# --- Google Routes -------------------------------------------------------------------------------


async def test_routes_request_shape_and_response_mapping(respx_mock):
    route = google(respx_mock, static_s=1200, traffic_s=1800)
    provider = GoogleRoutesTrafficProvider("test-key", timezones=FixedZone(), now=now)

    estimate = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18)

    assert route.call_count == 2
    first, second = bodies(route)
    for body in (first, second):
        assert body["travelMode"] == "DRIVE"
        assert body["origin"] == {"location": {"latLng": {"latitude": 30.40, "longitude": -97.70}}}
        assert body["destination"] == {"location": {"latLng": {"latitude": 30.27, "longitude": -97.74}}}
    assert first["routingPreference"] == "TRAFFIC_UNAWARE" and "departureTime" not in first
    assert second["routingPreference"] == "TRAFFIC_AWARE_OPTIMAL"
    # 18:00 Chicago (CST) is 00:00Z; leaving 20 static minutes earlier gives 23:40Z the day before.
    assert second["departureTime"] == "2026-11-12T23:40:00Z"
    headers = route.calls.last.request.headers
    assert headers["X-Goog-Api-Key"] == "test-key"
    assert headers["X-Goog-FieldMask"] == "routes.duration,routes.staticDuration,routes.distanceMeters"

    assert estimate is not None and estimate.basis == "routes_api"
    assert estimate.typical_minutes == 20.0 and estimate.event_time_minutes == 30.0
    assert estimate.distance_km == 18.2 and estimate.delay_ratio == 1.5
    assert estimate.rush_hour_overlap is True
    assert estimate.origin == "30.40000,-97.70000"
    assert "arrive Thu 12 Nov 18:00" in estimate.notes and "in the past" not in estimate.notes


async def test_routes_accepts_address_origin_and_aware_arrival(respx_mock):
    route = google(respx_mock)
    provider = GoogleRoutesTrafficProvider("k", timezones=FixedZone(), now=now)
    arrive_utc = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)  # same instant as Thursday 18:00 Chicago

    estimate = await provider.estimate_travel("Round Rock, TX", VENUE, arrive_by=arrive_utc)

    assert bodies(route)[0]["origin"] == {"address": "Round Rock, TX"}
    assert bodies(route)[1]["departureTime"] == "2026-11-12T23:40:00Z"
    assert estimate.origin == "Round Rock, TX" and estimate.rush_hour_overlap is True


async def test_past_arrival_moves_to_next_same_weekday(respx_mock):
    route = google(respx_mock)
    provider = GoogleRoutesTrafficProvider("k", timezones=FixedZone(), now=now)
    last_monday = datetime(2026, 11, 2, 18, 0)

    estimate = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=last_monday)

    assert bodies(route)[1]["departureTime"] == "2026-11-09T23:40:00Z"  # Monday 17:40 CST, in the future
    assert "in the past" in estimate.notes and "next Monday at 18:00" in estimate.notes
    assert estimate.event_time_minutes == 30.0


async def test_imminent_arrival_whose_departure_already_passed_is_shifted_a_week(respx_mock):
    route = google(respx_mock, static_s=3600)
    provider = GoogleRoutesTrafficProvider("k", timezones=FixedZone(), now=now)
    in_half_an_hour = (NOW + timedelta(minutes=30)).astimezone(CHICAGO).replace(tzinfo=None)

    estimate = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=in_half_an_hour)

    departure = datetime.fromisoformat(bodies(route)[1]["departureTime"].replace("Z", "+00:00"))
    assert departure == NOW + timedelta(days=7, minutes=30) - timedelta(hours=1)
    assert "in the past" in estimate.notes


async def test_no_arrival_time_gives_typical_only(respx_mock):
    route = google(respx_mock)
    provider = GoogleRoutesTrafficProvider("k", timezones=FixedZone(), now=now)

    estimate = await provider.estimate_travel(ORIGIN, VENUE)

    assert route.call_count == 1 and bodies(route)[0]["routingPreference"] == "TRAFFIC_UNAWARE"
    assert estimate.typical_minutes == 20.0 and estimate.event_time_minutes is None
    assert estimate.delay_ratio is None and estimate.rush_hour_overlap is False
    assert "No arrival time" in estimate.notes


async def test_no_route_found_returns_estimate_without_minutes(respx_mock):
    respx_mock.post(ROUTES_URL).mock(return_value=httpx.Response(200, json={}))
    provider = GoogleRoutesTrafficProvider("k", timezones=FixedZone(), now=now, fallback=HeuristicTrafficProvider())

    estimate = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18)

    assert estimate.basis == "routes_api" and estimate.typical_minutes is None
    assert estimate.rush_hour_overlap is True and "no drivable route" in estimate.notes


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "API disabled"}}),
        httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}),
        httpx.TimeoutException("timed out"),
    ],
)
async def test_google_failure_falls_back_to_heuristic(respx_mock, failure):
    if isinstance(failure, Exception):
        respx_mock.post(ROUTES_URL).mock(side_effect=failure)
    else:
        respx_mock.post(ROUTES_URL).mock(return_value=failure)
    zones = FixedZone()
    provider = GoogleRoutesTrafficProvider(
        "k", timezones=zones, now=now, fallback=HeuristicTrafficProvider(timezones=zones)
    )

    estimate = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18)

    assert estimate is not None and estimate.basis == "heuristic"
    assert estimate.notes.startswith("Live traffic was unavailable")
    assert estimate.event_time_minutes > estimate.typical_minutes and estimate.rush_hour_overlap is True
    assert "API disabled" not in estimate.notes  # upstream detail goes to the log, not the card


async def test_google_failure_without_fallback_returns_none(respx_mock):
    respx_mock.post(ROUTES_URL).mock(return_value=httpx.Response(500, text="boom"))
    provider = GoogleRoutesTrafficProvider("k", timezones=FixedZone(), now=now)

    assert await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18) is None


# --- heuristic -----------------------------------------------------------------------------------


async def test_heuristic_weekday_peak_vs_weekend():
    provider = HeuristicTrafficProvider()

    peak = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18)
    weekend = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=SATURDAY_18)
    midday = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18.replace(hour=13))
    night = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=THURSDAY_18.replace(hour=23))

    assert peak.basis == "heuristic" and peak.distance_km == pytest.approx(19.4, abs=0.1)
    assert peak.typical_minutes == weekend.typical_minutes == midday.typical_minutes == night.typical_minutes
    assert peak.delay_ratio == 1.55 and peak.rush_hour_overlap is True
    assert peak.event_time_minutes == pytest.approx(peak.typical_minutes * 1.55, abs=0.1)
    assert weekend.delay_ratio == 1.1 and weekend.rush_hour_overlap is False
    assert midday.delay_ratio == 1.1 and midday.rush_hour_overlap is False
    assert night.delay_ratio == 1.0 and night.event_time_minutes == night.typical_minutes
    assert "not live traffic" in peak.notes


async def test_heuristic_without_arrival_or_with_coordinate_string():
    provider = HeuristicTrafficProvider()

    plain = await provider.estimate_travel(ORIGIN, VENUE)
    from_text = await provider.estimate_travel("30.40, -97.70", VENUE, arrive_by=THURSDAY_18)

    assert plain.typical_minutes and plain.event_time_minutes is None and plain.rush_hour_overlap is False
    assert from_text.typical_minutes == plain.typical_minutes and from_text.rush_hour_overlap is True


async def test_heuristic_unlocatable_address_judges_rush_hour_from_arrival_only():
    provider = HeuristicTrafficProvider()

    peak = await provider.estimate_travel("Round Rock, TX", VENUE, arrive_by=THURSDAY_18)
    off_peak = await provider.estimate_travel("Round Rock, TX", VENUE, arrive_by=SATURDAY_18)

    assert peak.typical_minutes is None and peak.event_time_minutes is None and peak.distance_km is None
    assert peak.rush_hour_overlap is True and off_peak.rush_hour_overlap is False
    assert peak.origin == "Round Rock, TX" and "Could not locate" in peak.notes


async def test_heuristic_converts_aware_arrival_to_venue_time():
    zones = FixedZone()
    provider = HeuristicTrafficProvider(timezones=zones)
    seven_pm_chicago = datetime(2026, 11, 13, 1, 0, tzinfo=UTC)

    aware = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=seven_pm_chicago)
    naive = await provider.estimate_travel(ORIGIN, VENUE, arrive_by=datetime(2026, 11, 12, 19, 0))

    assert zones.calls == 1  # the naive arrival is already venue-local, so no lookup
    assert aware.rush_hour_overlap is True and aware.delay_ratio == naive.delay_ratio == 1.55
    assert "Thu 12 Nov 19:00" in aware.notes


def test_free_flow_time_grows_with_distance():
    minutes = [heuristic_free_flow_minutes(km) for km in (0.5, 2, 5, 12, 20, 45, 60, 120, 150, 300)]
    assert minutes == sorted(minutes)
    assert heuristic_free_flow_minutes(5) == 10.0  # 5 km at 30 km/h
    assert heuristic_free_flow_minutes(150) == 100.0  # 150 km at 90 km/h


def test_peak_overlap_and_multiplier_bands():
    thursday = datetime(2026, 11, 12, 7, 0)
    assert overlaps_weekday_peak(thursday, 30) is False  # arrives exactly as the peak starts
    assert overlaps_weekday_peak(thursday.replace(minute=10), 30) is True
    assert overlaps_weekday_peak(thursday.replace(hour=10), 60) is True  # 09:00-10:00 clips the morning peak
    assert overlaps_weekday_peak(thursday.replace(hour=11), 60) is False
    assert overlaps_weekday_peak(datetime(2026, 11, 14, 18, 0), 30) is False  # Saturday
    assert overlaps_weekday_peak(datetime(2026, 11, 17, 1, 0), 9 * 60) is True  # Monday 16:00 -> Tuesday 01:00
    assert overlaps_weekday_peak(datetime(2026, 11, 16, 1, 0), 9 * 60) is False  # Sunday 16:00 -> Monday 01:00
    assert traffic_multiplier(thursday.replace(hour=8)) == 1.45
    assert traffic_multiplier(thursday.replace(hour=17)) == 1.55
    assert traffic_multiplier(thursday.replace(hour=12)) == 1.1
    assert traffic_multiplier(thursday.replace(hour=3)) == 1.0
    assert traffic_multiplier(datetime(2026, 11, 14, 12, 0)) == 1.1


# --- timezone resolution -------------------------------------------------------------------------


async def test_timezone_resolver_uses_open_meteo_and_caches(respx_mock):
    route = respx_mock.get(OPEN_METEO_FORECAST_URL).mock(
        return_value=httpx.Response(200, json={"timezone": "America/Chicago", "utc_offset_seconds": -21600})
    )
    resolver = TimezoneResolver()

    first = await resolver.resolve(VENUE)
    second = await resolver.resolve(GeoPoint(lat=30.271, lng=-97.741))

    assert first == second == CHICAGO and route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["timezone"] == "auto" and float(params["latitude"]) == 30.27


async def test_timezone_resolver_falls_back_and_retries(respx_mock):
    route = respx_mock.get(OPEN_METEO_FORECAST_URL).mock(return_value=httpx.Response(503))
    resolver = TimezoneResolver()

    zone = await resolver.resolve(VENUE)
    await resolver.resolve(VENUE)

    assert zone == longitude_timezone(VENUE.lng) == timezone(timedelta(hours=-7))
    assert route.call_count == 2  # failures are not cached


async def test_timezone_resolver_uses_offset_when_zone_name_is_unknown(respx_mock):
    respx_mock.get(OPEN_METEO_FORECAST_URL).mock(
        return_value=httpx.Response(200, json={"timezone": "Mars/Olympus", "utc_offset_seconds": 3600})
    )

    assert await TimezoneResolver().resolve(GeoPoint(lat=48.9, lng=2.3)) == timezone(timedelta(hours=1))


async def test_offline_resolver_never_calls_the_network(respx_mock):
    assert await TimezoneResolver(online=False).resolve(GeoPoint(lat=35.7, lng=139.7)) == timezone(timedelta(hours=9))
    assert not respx_mock.calls


def test_haversine():
    assert haversine_km(GeoPoint(lat=0, lng=0), GeoPoint(lat=0, lng=1)) == pytest.approx(111.2, abs=0.1)
    assert haversine_km(VENUE, VENUE) == 0


# --- factory -------------------------------------------------------------------------------------


def test_build_traffic_provider(settings):
    assert isinstance(build_traffic_provider(settings), HeuristicTrafficProvider)

    auto = settings.model_copy(update={"traffic_provider": "auto"})
    assert isinstance(build_traffic_provider(auto), HeuristicTrafficProvider)

    with_key = settings.model_copy(update={"traffic_provider": "auto", "google_maps_api_key": "k"})
    provider = build_traffic_provider(with_key)
    assert isinstance(provider, GoogleRoutesTrafficProvider)
    assert isinstance(provider._fallback, HeuristicTrafficProvider)

    with pytest.raises(ProviderError):
        build_traffic_provider(settings.model_copy(update={"traffic_provider": "google"}))
