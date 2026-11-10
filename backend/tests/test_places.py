"""Places provider: Google (respx-mocked), mock sample data, factory selection."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.config import Settings
from app.providers import places
from app.providers.base import GeoPoint, PlaceCandidate, PlaceDetails, ProviderError
from app.providers.places import (
    GooglePlacesProvider,
    MockPlacesProvider,
    build_places_provider,
    haversine_m,
)

KEY = "AIza-test-key-not-real"
SEARCH_URL = f"{places.PLACES_API}/places:searchText"
NEARBY_URL = f"{places.PLACES_API}/places:searchNearby"
AUSTIN = GeoPoint(lat=30.2672, lng=-97.7431)


def google_error(code: int, status: str, message: str, reason: str | None = None) -> httpx.Response:
    error: dict = {"code": code, "message": message, "status": status}
    if reason:
        error["details"] = [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "domain": "googleapis.com"}
        ]
    return httpx.Response(code, json={"error": error})


SERVICE_DISABLED = google_error(
    403, "PERMISSION_DENIED",
    "Places API (New) has not been used in project 123 before or it is disabled. Enable it by visiting "
    "https://console.developers.google.com/apis/api/places.googleapis.com/overview?project=123",
    reason="SERVICE_DISABLED",
)  # fmt: skip

VENUE = {
    "id": "ChIJvenue1",
    "displayName": {"text": "Palmer Events Center", "languageCode": "en"},
    "formattedAddress": "900 Barton Springs Rd, Austin, TX 78704, USA",
    "location": {"latitude": 30.2593, "longitude": -97.7515},
    "types": ["event_venue", "point_of_interest"],
    "rating": 4.6,
    "userRatingCount": 2210,
    "priceLevel": "PRICE_LEVEL_MODERATE",
    "websiteUri": "https://www.palmereventscenter.com/",
    "googleMapsUri": "https://maps.google.com/?cid=1",
}

DETAILS = {
    **VENUE,
    "photos": [{"name": f"places/ChIJvenue1/photos/photo{i}", "widthPx": 4000, "heightPx": 3000} for i in range(5)],
    "internationalPhoneNumber": "+1 512-404-4500",
    "regularOpeningHours": {
        "weekdayDescriptions": [
            "Monday: 8:00 AM – 5:00 PM",
            "Tuesday: 8:00 AM – 5:00 PM",
            "Wednesday: 8:00 AM – 5:00 PM",
            "Thursday: 8:00 AM – 5:00 PM",
            "Friday: 8:00 AM – 5:00 PM",
            "Saturday: Closed",
            "Sunday: Closed",
        ]
    },  # fmt: skip
    "editorialSummary": {"text": "Large exhibition halls near Lady Bird Lake.", "languageCode": "en"},
    "reviews": [
        {"rating": 5, "text": {"text": "Loads of space and " + "x" * 400, "languageCode": "en"}},
        {"rating": 3, "originalText": {"text": "Parking fills up fast.", "languageCode": "en"}},
        {"rating": 4, "text": {"text": "", "languageCode": "en"}},
        {"rating": 4, "text": {"text": "Fourth review.", "languageCode": "en"}},
        {"rating": 2, "text": {"text": "Fifth review.", "languageCode": "en"}},
    ],
    "parkingOptions": {"paidParkingLot": True, "freeStreetParking": False, "valetParking": True},
    "accessibilityOptions": {"wheelchairAccessibleEntrance": True, "wheelchairAccessibleParking": True},
}


@pytest.fixture
def google():
    provider = GooglePlacesProvider(KEY, timeout_s=2.0)
    yield provider


@pytest.fixture(autouse=True)
async def close_provider(google):
    yield
    await google.aclose()


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def photo_route(mock_api: respx.MockRouter) -> respx.Route:
    return mock_api.get(url__regex=r"https://places\.googleapis\.com/v1/places/[^/]+/photos/[^/]+/media.*").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={"name": "x", "photoUri": f"https://lh3.googleusercontent.com/p/{request.url.path.split('/')[-2]}"},
        )
    )


def assert_key_only_in_header(mock_api: respx.MockRouter) -> None:
    for call in mock_api.calls:
        assert KEY not in str(call.request.url)
        assert call.request.headers["X-Goog-Api-Key"] == KEY


# ----------------------------------------------------------------------------- search_venues


async def test_search_venues_biases_to_location_and_maps_fields(google, mock_api):
    route = mock_api.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"places": [VENUE]}))

    found = await google.search_venues("conference venue for 200", location=AUSTIN, radius_m=70_000, max_results=5)

    body = json.loads(route.calls.last.request.content)
    assert body["textQuery"] == "conference venue for 200"
    assert body["pageSize"] == 5
    assert body["locationBias"]["circle"]["center"] == {"latitude": 30.2672, "longitude": -97.7431}
    assert body["locationBias"]["circle"]["radius"] == 50_000.0  # clamped to Google's maximum
    assert route.calls.last.request.headers["X-Goog-FieldMask"] == places.SEARCH_FIELDS
    assert "places.photos" not in places.SEARCH_FIELDS and "places.reviews" not in places.SEARCH_FIELDS
    assert_key_only_in_header(mock_api)

    (candidate,) = found
    assert candidate.place_id == "ChIJvenue1" and candidate.name == "Palmer Events Center"
    assert candidate.location == GeoPoint(lat=30.2593, lng=-97.7515)
    assert candidate.rating == 4.6 and candidate.review_count == 2210 and candidate.price_level == "moderate"
    assert candidate.website == VENUE["websiteUri"] and candidate.maps_url == VENUE["googleMapsUri"]
    assert candidate.types == ["event_venue", "point_of_interest"] and candidate.photo_urls == []


async def test_search_venues_folds_near_into_query_without_location(google, mock_api):
    route = mock_api.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"places": [VENUE]}))
    await google.search_venues("rooftop bar", near="Austin, TX")
    body = json.loads(route.calls.last.request.content)
    assert body["textQuery"] == "rooftop bar near Austin, TX" and "locationBias" not in body

    await google.search_venues("rooftop bar in Austin, TX", near="Austin, TX")
    assert json.loads(route.calls.last.request.content)["textQuery"] == "rooftop bar in Austin, TX"


async def test_search_venues_empty_results(google, mock_api):
    mock_api.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
    assert await google.search_venues("something nobody offers", near="Nowhere") == []
    assert await google.search_venues("   ") == []


async def test_search_venues_api_not_enabled_says_what_to_enable(google, mock_api):
    mock_api.post(SEARCH_URL).mock(return_value=SERVICE_DISABLED)
    with pytest.raises(ProviderError) as info:
        await google.search_venues("venue", near="Austin")
    assert "Places API (New)" in str(info.value) and "Enable" in str(info.value)


async def test_search_venues_invalid_key_points_at_env(google, mock_api):
    mock_api.post(SEARCH_URL).mock(
        return_value=google_error(400, "INVALID_ARGUMENT", "API key not valid.", reason="API_KEY_INVALID")
    )
    with pytest.raises(ProviderError, match="GOOGLE_MAPS_API_KEY"):
        await google.search_venues("venue", near="Austin")


@pytest.mark.parametrize(
    "response",
    [
        google_error(403, "PERMISSION_DENIED", "blocked", reason="API_KEY_SERVICE_BLOCKED"),
        google_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"),
        httpx.Response(503, text="<html>upstream unavailable</html>"),
    ],
)
async def test_search_venues_failures_raise_provider_error(google, mock_api, response):
    mock_api.post(SEARCH_URL).mock(return_value=response)
    with pytest.raises(ProviderError):
        await google.search_venues("venue", near="Austin")


async def test_search_venues_timeout_raises_provider_error(google, mock_api):
    mock_api.post(SEARCH_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="timed out"):
        await google.search_venues("venue", near="Austin")


# ----------------------------------------------------------------------------- get_details


async def test_get_details_maps_everything_and_resolves_keyless_photos(google, mock_api):
    details_route = mock_api.get(f"{places.PLACES_API}/places/ChIJvenue1").mock(
        return_value=httpx.Response(200, json=DETAILS)
    )
    photos = photo_route(mock_api)

    details = await google.get_details("ChIJvenue1")

    assert isinstance(details, PlaceDetails)
    assert details_route.calls.last.request.headers["X-Goog-FieldMask"] == places.DETAILS_FIELDS
    assert details.name == "Palmer Events Center" and details.phone == "+1 512-404-4500"
    assert len(details.opening_hours) == 7 and details.opening_hours[5] == "Saturday: Closed"
    assert details.editorial_summary == "Large exhibition halls near Lady Bird Lake."
    assert details.parking_features == ["Paid parking lot", "No free street parking", "Valet parking"]
    assert details.accessibility_features == ["Wheelchair-accessible parking", "Wheelchair-accessible entrance"]
    assert details.capacity_hint is None
    assert len(details.review_snippets) == 3  # capped; blank review skipped
    assert details.review_snippets[0].startswith("5/5 — Loads of space") and details.review_snippets[0].endswith("…")
    assert details.review_snippets[1] == "3/5 — Parking fills up fast."

    assert photos.call_count == 3  # of 5 available
    assert details.photo_urls == [f"https://lh3.googleusercontent.com/p/photo{i}" for i in range(3)]
    for call in photos.calls:
        assert call.request.url.params["skipHttpRedirect"] == "true"
        assert call.request.url.params["maxWidthPx"] == str(places.PHOTO_MAX_WIDTH_PX)
    assert_key_only_in_header(mock_api)
    assert KEY not in details.model_dump_json()


async def test_get_details_accepts_resource_name_and_survives_photo_failure(google, mock_api):
    mock_api.get(f"{places.PLACES_API}/places/ChIJvenue1").mock(return_value=httpx.Response(200, json=DETAILS))
    mock_api.get(url__regex=r".*/photos/photo0/media.*").mock(return_value=httpx.Response(500))
    mock_api.get(url__regex=r".*/photos/photo1/media.*").mock(side_effect=httpx.ConnectTimeout("x"))
    mock_api.get(url__regex=r".*/photos/photo2/media.*").mock(
        return_value=httpx.Response(200, json={"name": "n", "photoUri": "https://lh3.googleusercontent.com/ok"})
    )
    details = await google.get_details("places/ChIJvenue1")
    assert details is not None and details.photo_urls == ["https://lh3.googleusercontent.com/ok"]


@pytest.mark.parametrize(
    "response",
    [
        google_error(404, "NOT_FOUND", "Place not found"),
        google_error(400, "INVALID_ARGUMENT", "Not a valid Place ID: nope"),
    ],
)
async def test_get_details_unknown_place_is_none(google, mock_api, response):
    mock_api.get(url__regex=r".*/v1/places/[^/]+$").mock(return_value=response)
    assert await google.get_details("nope") is None
    assert await google.get_details("  ") is None


async def test_get_details_access_failure_raises(google, mock_api):
    mock_api.get(url__regex=r".*/v1/places/[^/]+$").mock(return_value=SERVICE_DISABLED)
    with pytest.raises(ProviderError, match="Places API \\(New\\)"):
        await google.get_details("ChIJvenue1")


# ----------------------------------------------------------------------------- find_parking


async def test_find_parking_sorts_nearest_first_with_distances(google, mock_api):
    far = {
        "displayName": {"text": "City Garage"},
        "formattedAddress": "2 Far St",
        "location": {"latitude": 30.2672, "longitude": -97.7380},  # ~490 m east
        "types": ["parking_garage", "parking"],
        "rating": 3.9,
    }
    near = {
        "displayName": {"text": "Surface Lot"},
        "location": {"latitude": 30.2690, "longitude": -97.7431},  # ~200 m north
        "types": ["parking_lot", "parking"],
    }
    nowhere = {"displayName": {"text": "Valet stand"}, "types": ["parking"]}
    route = mock_api.post(NEARBY_URL).mock(return_value=httpx.Response(200, json={"places": [far, nowhere, near]}))

    options = await google.find_parking(AUSTIN, radius_m=600, max_results=5)

    body = json.loads(route.calls.last.request.content)
    assert body["includedTypes"] == places.PARKING_TYPES and body["rankPreference"] == "DISTANCE"
    assert body["locationRestriction"]["circle"]["radius"] == 600.0 and body["maxResultCount"] == 5
    assert route.calls.last.request.headers["X-Goog-FieldMask"] == places.PARKING_FIELDS

    assert [o.name for o in options] == ["Surface Lot", "City Garage", "Valet stand"]
    assert [o.kind for o in options] == ["lot", "garage", "valet"]
    assert 190 <= options[0].distance_m <= 210 and 480 <= options[1].distance_m <= 500
    assert options[2].distance_m is None and options[1].rating == 3.9


async def test_find_parking_empty_and_failures(google, mock_api):
    route = mock_api.post(NEARBY_URL).mock(return_value=httpx.Response(200, json={}))
    assert await google.find_parking(AUSTIN) == []
    route.mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError, match="unavailable"):
        await google.find_parking(AUSTIN)


# ----------------------------------------------------------------------------- geocode


async def test_geocode_uses_geocoding_api(google, mock_api):
    route = mock_api.get(places.GEOCODING_API).mock(
        return_value=httpx.Response(200, json={"results": [{"location": {"latitude": 30.2672, "longitude": -97.7431}}]})
    )
    assert await google.geocode("Austin, TX") == AUSTIN
    request = route.calls.last.request
    assert request.url.params["addressQuery"] == "Austin, TX"
    assert request.headers["X-Goog-FieldMask"] == places.GEOCODE_FIELDS
    assert_key_only_in_header(mock_api)


async def test_geocode_no_match_is_none(google, mock_api):
    mock_api.get(places.GEOCODING_API).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await google.geocode("asdfghjkl") is None
    assert await google.geocode("") is None


async def test_geocode_falls_back_to_text_search_when_api_not_enabled(google, mock_api):
    geocoding = mock_api.get(places.GEOCODING_API).mock(
        return_value=google_error(403, "PERMISSION_DENIED", "Geocoding API has not been used...", "SERVICE_DISABLED")
    )
    search = mock_api.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"places": [{"location": {"latitude": 1.5, "longitude": 2.5}}]})
    )
    assert await google.geocode("Austin, TX") == GeoPoint(lat=1.5, lng=2.5)
    body = json.loads(search.calls.last.request.content)
    assert body == {"textQuery": "Austin, TX", "pageSize": 1}
    assert search.calls.last.request.headers["X-Goog-FieldMask"] == places.LOCATION_ONLY_FIELDS

    assert await google.geocode("Dallas, TX") == GeoPoint(lat=1.5, lng=2.5)
    assert geocoding.call_count == 1 and search.call_count == 2  # the 403 is remembered

    search.mock(return_value=SERVICE_DISABLED)
    with pytest.raises(ProviderError):
        await google.geocode("Houston, TX")


async def test_geocode_other_failures_raise(google, mock_api):
    mock_api.get(places.GEOCODING_API).mock(return_value=httpx.Response(502))
    with pytest.raises(ProviderError):
        await google.geocode("Austin, TX")


# ----------------------------------------------------------------------------- mock provider


async def test_mock_search_is_deterministic_and_labelled():
    mock = MockPlacesProvider()
    first = await mock.search_venues("Conference venue for 200 people", near="Austin, TX", max_results=6)
    again = await mock.search_venues("conference venue for 200 people", near="Austin, TX", max_results=6)
    other = await mock.search_venues("wedding garden", near="Austin, TX", max_results=6)

    assert first == again and len(first) == 6
    assert [c.place_id for c in first] != [c.place_id for c in other]
    for candidate in first:
        assert candidate.place_id.startswith("mock_") and candidate.name.startswith("[Sample]")
        assert candidate.location is not None and haversine_m(candidate.location, AUSTIN) < 15_000
        assert "Austin, TX" in (candidate.address or "")
        assert "convention_center" in candidate.types and candidate.photo_urls == []
    assert all("wedding_venue" in c.types for c in other)


async def test_mock_details_and_parking_agree_with_search():
    mock = MockPlacesProvider()
    (candidate,) = await mock.search_venues("gala dinner", location=GeoPoint(lat=51.5, lng=-0.12), max_results=1)

    details = await mock.get_details(candidate.place_id)
    assert details is not None
    assert details.model_dump(include=set(PlaceCandidate.model_fields)) == candidate.model_dump()
    assert details.name.startswith("[Sample]") and "not a real venue" in (details.editorial_summary or "")
    assert len(details.opening_hours) == 7 and details.parking_features and details.capacity_hint
    assert details.website and details.website.startswith("https://example.com/")

    parking = await mock.find_parking(candidate.location, radius_m=500, max_results=3)
    assert parking == await mock.find_parking(candidate.location, radius_m=500, max_results=3)
    assert len(parking) == 3 and all(p.name.startswith("[Sample]") for p in parking)
    distances = [p.distance_m for p in parking]
    assert distances == sorted(distances) and all(0 < d <= 500 for d in distances)
    for option in parking:
        assert option.kind in {"lot", "garage", "street", "valet"}
        assert abs(haversine_m(candidate.location, option.location) - option.distance_m) < 2

    assert await mock.get_details("mock_bogus") is None
    assert await mock.get_details("ChIJ-real-looking") is None
    assert await mock.search_venues("", near="x") == []


async def test_mock_geocode_knows_cities_and_is_stable_elsewhere():
    mock = MockPlacesProvider()
    assert await mock.geocode("Downtown Austin, Texas") == AUSTIN
    point = await mock.geocode("12 Nowhere Lane, Smalltown")
    assert point == await mock.geocode("12 nowhere lane,  smalltown") and point != AUSTIN
    assert await mock.geocode("") is None


# ----------------------------------------------------------------------------- factory


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{"google_maps_api_key": None, "google_api_key": None, **overrides})


def test_build_places_provider_selection():
    assert isinstance(build_places_provider(make_settings(places_provider="auto")), MockPlacesProvider)
    assert isinstance(
        build_places_provider(make_settings(places_provider="mock", google_api_key="k")), MockPlacesProvider
    )
    auto = build_places_provider(make_settings(places_provider="auto", google_api_key="k"))
    assert isinstance(auto, GooglePlacesProvider) and auto.name == "google"
    assert isinstance(
        build_places_provider(make_settings(places_provider="google", google_maps_api_key="k")), GooglePlacesProvider
    )
    with pytest.raises(ProviderError, match="GOOGLE_MAPS_API_KEY"):
        build_places_provider(make_settings(places_provider="google"))


async def test_context_builds_mock_provider_under_test_settings(ctx):
    assert isinstance(ctx.places, MockPlacesProvider) and ctx.places.name == "mock"
