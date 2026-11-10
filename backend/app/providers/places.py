"""Places provider (venue search, details, nearby parking, geocoding).

`GooglePlacesProvider` talks to the Places API (New) and the Geocoding API v4;
`MockPlacesProvider` serves deterministic, clearly-labelled sample data for
keyless development and tests. `build_places_provider` picks one from settings
and never swaps Google for the mock after a Google failure: a fake venue
presented as real would mislead a planner, so failures surface as
`ProviderError`.

The API key is only ever sent in the `X-Goog-Api-Key` header. It is never put
in a URL, so it cannot leak through returned models or request logs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.providers import mock_places_data as sample
from app.providers.base import GeoPoint, ParkingOption, PlaceCandidate, PlaceDetails, PlacesProvider, ProviderError

log = logging.getLogger(__name__)

PLACES_API = "https://places.googleapis.com/v1"
GEOCODING_API = "https://geocode.googleapis.com/v4/geocode/address"
PLACES_API_NAME = "Places API (New)"
GEOCODING_API_NAME = "Geocoding API"

# Field masks decide the billing SKU, so each one lists exactly what we map.
# Text Search: rating/userRatingCount/priceLevel/websiteUri lift it from Pro to Enterprise.
SEARCH_FIELDS = ",".join(
    f"places.{f}"
    for f in (
        "id", "displayName", "formattedAddress", "location", "types",
        "rating", "userRatingCount", "priceLevel", "websiteUri", "googleMapsUri",
    )
)  # fmt: skip
# Place Details: editorialSummary/reviews/parkingOptions put it in Enterprise + Atmosphere.
DETAILS_FIELDS = ",".join(
    (
        "id", "displayName", "formattedAddress", "location", "types",
        "rating", "userRatingCount", "priceLevel", "websiteUri", "googleMapsUri", "photos",
        "internationalPhoneNumber", "regularOpeningHours", "editorialSummary", "reviews",
        "parkingOptions", "accessibilityOptions",
    )
)  # fmt: skip
# Nearby Search: `rating` lifts it from Pro to Enterprise (drop it to stay in Pro).
PARKING_FIELDS = "places.displayName,places.formattedAddress,places.location,places.types,places.rating"
GEOCODE_FIELDS = "results.location"
LOCATION_ONLY_FIELDS = "places.location"

PARKING_TYPES = ["parking", "parking_garage", "parking_lot"]
MAX_PHOTOS = 3
PHOTO_MAX_WIDTH_PX = 1600
MAX_REVIEW_SNIPPETS = 3
REVIEW_SNIPPET_CHARS = 280

PARKING_OPTION_LABELS = {
    "freeParkingLot": "Free parking lot",
    "paidParkingLot": "Paid parking lot",
    "freeStreetParking": "Free street parking",
    "paidStreetParking": "Paid street parking",
    "valetParking": "Valet parking",
    "freeGarageParking": "Free garage parking",
    "paidGarageParking": "Paid garage parking",
}
ACCESSIBILITY_OPTION_LABELS = {
    "wheelchairAccessibleParking": "Wheelchair-accessible parking",
    "wheelchairAccessibleEntrance": "Wheelchair-accessible entrance",
    "wheelchairAccessibleRestroom": "Wheelchair-accessible restroom",
    "wheelchairAccessibleSeating": "Wheelchair-accessible seating",
}


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in metres."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = phi2 - phi1
    dlmb = math.radians(b.lng - a.lng)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _offset(origin: GeoPoint, distance_m: float, bearing_rad: float) -> GeoPoint:
    """Point `distance_m` from `origin` along `bearing_rad` (flat-earth approximation, fine under 50 km)."""
    dlat = distance_m * math.cos(bearing_rad) / 111_320
    dlng = distance_m * math.sin(bearing_rad) / (111_320 * max(math.cos(math.radians(origin.lat)), 0.01))
    return GeoPoint(lat=round(origin.lat + dlat, 5), lng=round(origin.lng + dlng, 5))


# --------------------------------------------------------------------------- Google


class GooglePlacesProvider:
    """Places API (New) + Geocoding API v4.

    Lookups that have a sensible empty answer return `[]` / `None`; anything that
    means "the lookup itself failed" (bad key, API not enabled, quota, 5xx,
    timeout) raises `ProviderError` whose message says what to fix.
    """

    name = "google"

    def __init__(self, api_key: str, *, timeout_s: float = 10.0) -> None:
        if not api_key:
            raise ProviderError("GooglePlacesProvider needs an API key")
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_s, connect=5.0)
        self._client: httpx.AsyncClient | None = None
        self._geocoding_unavailable = False  # set after a 403 so we stop paying for a failing call

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, headers={"X-Goog-Api-Key": self._api_key})
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- PlacesProvider -----------------------------------------------------

    async def geocode(self, address: str) -> GeoPoint | None:
        """Geocoding API v4; falls back to a Text Search when that API is not enabled for the key."""
        address = address.strip()
        if not address:
            return None
        if not self._geocoding_unavailable:
            response = await self._request(
                "GET", GEOCODING_API, api=GEOCODING_API_NAME,
                params={"addressQuery": address}, headers={"X-Goog-FieldMask": GEOCODE_FIELDS},
            )  # fmt: skip
            if response.status_code == 403:
                self._geocoding_unavailable = True
                log.warning(
                    "Geocoding API unavailable for this key (%s); using Text Search instead", _summary(response)
                )
            else:
                self._raise_for_status(response, GEOCODING_API_NAME)
                results = response.json().get("results") or []
                return _latlng(results[0].get("location")) if results else None
        response = await self._search_text(address, {"pageSize": 1}, fields=LOCATION_ONLY_FIELDS)
        places = response.json().get("places") or []
        return _latlng(places[0].get("location")) if places else None

    async def search_venues(
        self,
        query: str,
        *,
        near: str | None = None,
        location: GeoPoint | None = None,
        radius_m: int = 15000,
        max_results: int = 8,
    ) -> list[PlaceCandidate]:
        """Text Search biased to `location`/`radius_m`, or with `near` folded into the query."""
        query = query.strip()
        if not query or max_results <= 0:
            return []
        body: dict[str, Any] = {"pageSize": min(max_results, 20)}
        if location is not None:
            body["locationBias"] = _circle(location, radius_m)
        elif near and near.strip().lower() not in query.lower():
            query = f"{query} near {near.strip()}"
        response = await self._search_text(query, body, fields=SEARCH_FIELDS)
        places = response.json().get("places") or []
        return [_candidate(p) for p in places[:max_results] if p.get("id")]

    async def get_details(self, place_id: str) -> PlaceDetails | None:
        """Place Details with up to `MAX_PHOTOS` keyless photo URLs; None when Google does not know the id."""
        place_id = place_id.strip().removeprefix("places/")
        if not place_id:
            return None
        response = await self._request(
            "GET", f"{PLACES_API}/places/{quote(place_id, safe='')}", api=PLACES_API_NAME,
            headers={"X-Goog-FieldMask": DETAILS_FIELDS},
        )  # fmt: skip
        if response.status_code == 404 or _is_bad_place_id(response):
            return None
        self._raise_for_status(response, PLACES_API_NAME)
        place = response.json()
        details = _details(place)
        details.photo_urls = await self._photo_urls(place.get("photos") or [])
        return details

    async def find_parking(
        self, location: GeoPoint, *, radius_m: int = 800, max_results: int = 5
    ) -> list[ParkingOption]:
        """Nearby Search for parking places, nearest first."""
        if max_results <= 0:
            return []
        body = {
            "includedTypes": PARKING_TYPES,
            "maxResultCount": min(max_results, 20),
            "rankPreference": "DISTANCE",
            "locationRestriction": _circle(location, radius_m),
        }
        response = await self._request(
            "POST", f"{PLACES_API}/places:searchNearby", api=PLACES_API_NAME,
            json=body, headers={"X-Goog-FieldMask": PARKING_FIELDS},
        )  # fmt: skip
        self._raise_for_status(response, PLACES_API_NAME)
        options = [_parking_option(p, location) for p in response.json().get("places") or []]
        options.sort(key=lambda o: (o.distance_m is None, o.distance_m or 0))
        return options[:max_results]

    # -- HTTP ---------------------------------------------------------------

    async def _search_text(self, query: str, body: dict[str, Any], *, fields: str) -> httpx.Response:
        response = await self._request(
            "POST", f"{PLACES_API}/places:searchText", api=PLACES_API_NAME,
            json={"textQuery": query, **body}, headers={"X-Goog-FieldMask": fields},
        )  # fmt: skip
        self._raise_for_status(response, PLACES_API_NAME)
        return response

    async def _request(self, method: str, url: str, *, api: str, **kwargs: Any) -> httpx.Response:
        """One request; transport-level failures become `ProviderError`. HTTP errors are left to the caller."""
        try:
            return await self._http().request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Google {api} timed out after {self._timeout.read}s; try again.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach Google {api}: {type(exc).__name__}") from exc

    async def _photo_urls(self, photos: list[dict[str, Any]]) -> list[str]:
        """Resolve photo resource names to Google's keyless `photoUri`s; a failed photo is skipped, not fatal."""
        names = [p["name"] for p in photos[:MAX_PHOTOS] if str(p.get("name", "")).startswith("places/")]
        uris = await asyncio.gather(*(self._photo_uri(name) for name in names))
        return [u for u in uris if u and self._api_key not in u]

    async def _photo_uri(self, name: str) -> str | None:
        try:
            response = await self._request(
                "GET", f"{PLACES_API}/{name}/media", api=PLACES_API_NAME,
                params={"maxWidthPx": PHOTO_MAX_WIDTH_PX, "skipHttpRedirect": "true"},
            )  # fmt: skip
        except ProviderError as exc:
            log.warning("photo %s skipped: %s", name, exc)
            return None
        if response.status_code != 200:
            log.warning("photo %s skipped: %s", name, _summary(response))
            return None
        return response.json().get("photoUri")

    def _raise_for_status(self, response: httpx.Response, api: str) -> None:
        if response.is_success:
            return
        status, message, reason = _google_error(response)
        message = message.replace(self._api_key, "***")  # belt and braces: Google does not echo keys
        if reason == "API_KEY_INVALID":
            hint = "Google rejected the Maps API key. Check GOOGLE_MAPS_API_KEY / GOOGLE_API_KEY in .env."
        elif reason == "SERVICE_DISABLED":
            hint = (
                f"{api} is not enabled for the Google Cloud project that owns this key. "
                f"Enable '{api}' under APIs & Services in the Cloud console, then retry."
            )
        elif reason == "API_KEY_SERVICE_BLOCKED":
            hint = f"The Maps API key's restrictions do not allow {api}. Allow it under Credentials > API restrictions."
        elif reason == "BILLING_DISABLED":
            hint = "Billing is not enabled on the Google Cloud project that owns this key."
        elif response.status_code == 429 or status == "RESOURCE_EXHAUSTED":
            hint = f"Google {api} quota exceeded; wait or raise the quota in the Cloud console."
        elif response.status_code in (401, 403):
            hint = (
                f"Google denied the {api} request. Check that the key is valid, {api} is enabled and billing is active."
            )
        elif response.status_code >= 500:
            hint = f"Google {api} is unavailable (HTTP {response.status_code}); try again shortly."
        else:
            hint = f"Google {api} rejected the request (HTTP {response.status_code})."
        raise ProviderError(f"{hint} Google said: {message or status or 'no details'}")


def _circle(center: GeoPoint, radius_m: int) -> dict[str, Any]:
    # Google accepts 0 < radius <= 50000.
    return {
        "circle": {
            "center": {"latitude": center.lat, "longitude": center.lng},
            "radius": float(min(max(radius_m, 1), 50_000)),
        }
    }


def _google_error(response: httpx.Response) -> tuple[str, str, str]:
    """(status, message, ErrorInfo reason) from a google.rpc.Status body; empty strings when absent."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return "", response.text[:200], ""
    reason = next(
        (d.get("reason", "") for d in error.get("details") or [] if str(d.get("@type", "")).endswith("ErrorInfo")), ""
    )
    return str(error.get("status", "")), str(error.get("message", "")).strip(), str(reason)


def _is_bad_place_id(response: httpx.Response) -> bool:
    """Google answers an unknown or malformed id with 400 INVALID_ARGUMENT "Not a valid Place ID: ..."."""
    if response.status_code != 400:
        return False
    _, message, reason = _google_error(response)
    return not reason and "place id" in message.lower()


def _summary(response: httpx.Response) -> str:
    status, message, reason = _google_error(response)
    return f"HTTP {response.status_code} {reason or status}: {message[:160]}"


def _latlng(obj: dict[str, Any] | None) -> GeoPoint | None:
    if not obj or "latitude" not in obj or "longitude" not in obj:
        return None
    return GeoPoint(lat=float(obj["latitude"]), lng=float(obj["longitude"]))


def _text(obj: dict[str, Any] | None) -> str | None:
    """`LocalizedText` → plain text."""
    return (obj or {}).get("text") or None


def _price_level(value: str | None) -> str | None:
    if not value or value == "PRICE_LEVEL_UNSPECIFIED":
        return None
    return value.removeprefix("PRICE_LEVEL_").lower().replace("_", " ")


def _candidate_fields(place: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": place["id"],
        "name": _text(place.get("displayName")) or place["id"],
        "address": place.get("formattedAddress"),
        "location": _latlng(place.get("location")),
        "types": list(place.get("types") or []),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "price_level": _price_level(place.get("priceLevel")),
        "website": place.get("websiteUri"),
        "maps_url": place.get("googleMapsUri"),
    }


def _candidate(place: dict[str, Any]) -> PlaceCandidate:
    return PlaceCandidate(**_candidate_fields(place))


def _flags(options: dict[str, Any] | None, labels: dict[str, str]) -> list[str]:
    """Google's boolean option objects as readable facts; a false is a fact too ("No valet parking")."""
    out: list[str] = []
    for key, label in labels.items():
        value = (options or {}).get(key)
        if value is True:
            out.append(label)
        elif value is False:
            out.append(f"No {label[0].lower()}{label[1:]}")
    return out


def _review_snippet(review: dict[str, Any]) -> str | None:
    text = " ".join((_text(review.get("text")) or _text(review.get("originalText")) or "").split())
    if not text:
        return None
    if len(text) > REVIEW_SNIPPET_CHARS:
        text = text[: REVIEW_SNIPPET_CHARS - 1].rstrip() + "…"
    rating = review.get("rating")
    return f"{rating:g}/5 — {text}" if isinstance(rating, int | float) else text


def _details(place: dict[str, Any]) -> PlaceDetails:
    snippets = [s for s in (_review_snippet(r) for r in place.get("reviews") or []) if s]
    return PlaceDetails(
        **_candidate_fields(place),
        phone=place.get("internationalPhoneNumber"),
        opening_hours=list((place.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []),
        editorial_summary=_text(place.get("editorialSummary")),
        review_snippets=snippets[:MAX_REVIEW_SNIPPETS],
        parking_features=_flags(place.get("parkingOptions"), PARKING_OPTION_LABELS),
        accessibility_features=_flags(place.get("accessibilityOptions"), ACCESSIBILITY_OPTION_LABELS),
        capacity_hint=None,  # Google does not report capacity
    )


def _parking_kind(place: dict[str, Any]) -> str | None:
    types = set(place.get("types") or [])
    name = (_text(place.get("displayName")) or "").lower()
    if "parking_garage" in types or "garage" in name:
        return "garage"
    if "valet" in name:
        return "valet"
    if "parking_lot" in types or "parking" in types:
        return "lot"
    return None


def _parking_option(place: dict[str, Any], origin: GeoPoint) -> ParkingOption:
    location = _latlng(place.get("location"))
    return ParkingOption(
        name=_text(place.get("displayName")) or "Parking",
        address=place.get("formattedAddress"),
        location=location,
        distance_m=round(haversine_m(origin, location)) if location else None,
        kind=_parking_kind(place),
        rating=place.get("rating"),
    )


# --------------------------------------------------------------------------- Mock


_MOCK_PREFIX = "mock_"
_COORD_SCALE = 100_000  # 1e-5 degrees ≈ 1 m


def _encode_coord(value: float, shift: int) -> str:
    n = int(round((value + shift) * _COORD_SCALE))
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def _decode_coord(token: str, shift: int) -> float:
    return round(int(token, 36) / _COORD_SCALE - shift, 5)


def _mock_place_id(kind: sample.VenueKind, location: GeoPoint) -> str:
    return f"{_MOCK_PREFIX}{kind.code}_{_encode_coord(location.lat, 90)}_{_encode_coord(location.lng, 180)}"


def _parse_mock_place_id(place_id: str) -> tuple[sample.VenueKind, GeoPoint] | None:
    parts = place_id.removeprefix(_MOCK_PREFIX).split("_")
    if not place_id.startswith(_MOCK_PREFIX) or len(parts) != 3:
        return None
    kind = next((k for k in sample.VENUE_KINDS if k.code == parts[0]), None)
    try:
        location = GeoPoint(lat=_decode_coord(parts[1], 90), lng=_decode_coord(parts[2], 180))
    except ValueError:
        return None
    if kind is None or not (-90 <= location.lat <= 90 and -180 <= location.lng <= 180):
        return None
    return kind, location


def _city_label(location: GeoPoint) -> str:
    label, distance = sample.UNKNOWN_CITY, 100_000.0
    for name, _aliases, lat, lng in sample.CITIES:
        d = haversine_m(location, GeoPoint(lat=lat, lng=lng))
        if d < distance:
            label, distance = name, d
    return label


def _mock_address(rng: random.Random, location: GeoPoint) -> str:
    return f"{rng.randint(100, 9899)} {rng.choice(sample.STREETS)}, {_city_label(location)}"


def _mock_venue(place_id: str) -> PlaceDetails | None:
    """Everything about a mock venue is a pure function of its id, so details always match search results."""
    parsed = _parse_mock_place_id(place_id)
    if parsed is None:
        return None
    kind, location = parsed
    rng = random.Random(place_id)  # str seeds hash deterministically across processes
    name = f"[Sample] {rng.choice(sample.NAME_STEMS)} {kind.noun}"
    rating = round(rng.uniform(3.8, 4.9), 1)
    opens, closes = rng.choice([("8:00 AM", "10:00 PM"), ("9:00 AM", "11:00 PM"), ("10:00 AM", "12:00 AM")])
    hours = [f"{day}: {opens} – {closes}" for day in sample.WEEKDAYS]
    return PlaceDetails(
        place_id=place_id,
        name=name,
        address=_mock_address(rng, location),
        location=location,
        types=list(kind.types),
        rating=rating,
        review_count=rng.randint(40, 2400),
        price_level=rng.choice(["inexpensive", "moderate", "expensive"]),
        website=f"https://example.com/venues/{place_id}",
        maps_url=f"https://www.google.com/maps/search/?api=1&query={location.lat},{location.lng}",
        photo_urls=[],
        phone=f"+1 555-01{rng.randint(0, 99):02d}",  # 555-01xx is reserved for fiction
        opening_hours=hours,
        editorial_summary=f"Sample data for offline development, not a real venue: {kind.blurb}.",
        review_snippets=rng.sample(sample.REVIEW_SNIPPETS, 2),
        parking_features=rng.sample(sample.PARKING_FEATURES, rng.randint(1, 3)),
        accessibility_features=rng.sample(sample.ACCESSIBILITY_FEATURES, rng.randint(2, 4)),
        capacity_hint=rng.choice([80, 120, 150, 200, 250, 300, 400, 600]),
    )


class MockPlacesProvider:
    """Offline sample data: deterministic for a given query, every name labelled "[Sample]"."""

    name = "mock"

    async def aclose(self) -> None:
        return None

    async def geocode(self, address: str) -> GeoPoint | None:
        """Known city names resolve to their real centre; anything else to a stable arbitrary point."""
        text = " ".join(address.lower().split())
        if not text:
            return None
        for _label, aliases, lat, lng in sample.CITIES:
            if any(alias in text for alias in aliases):
                return GeoPoint(lat=lat, lng=lng)
        rng = random.Random(f"geocode|{text}")
        return GeoPoint(lat=round(rng.uniform(30, 50), 4), lng=round(rng.uniform(-120, -75), 4))

    async def search_venues(
        self,
        query: str,
        *,
        near: str | None = None,
        location: GeoPoint | None = None,
        radius_m: int = 15000,
        max_results: int = 8,
    ) -> list[PlaceCandidate]:
        query = " ".join(query.lower().split())
        if not query or max_results <= 0:
            return []
        center = location or await self.geocode(near or query)
        assert center is not None
        rng = random.Random(f"search|{query}|{center.lat:.3f},{center.lng:.3f}")
        kinds = [k for k in sample.VENUE_KINDS if any(w in query for w in k.keywords)] or list(sample.VENUE_KINDS)
        spread = min(max(radius_m, 500), 20_000)
        results: dict[str, PlaceCandidate] = {}
        for _ in range(40):  # bounded in case of id collisions
            if len(results) >= min(max_results, 20):
                break
            kind = rng.choice(kinds)
            point = _offset(center, rng.uniform(0.1, 0.9) * spread, rng.uniform(0, math.tau))
            place_id = _mock_place_id(kind, point)
            details = _mock_venue(place_id)
            if details is not None:
                results[place_id] = PlaceCandidate(**details.model_dump(include=set(PlaceCandidate.model_fields)))
        return list(results.values())

    async def get_details(self, place_id: str) -> PlaceDetails | None:
        return _mock_venue(place_id.strip())

    async def find_parking(
        self, location: GeoPoint, *, radius_m: int = 800, max_results: int = 5
    ) -> list[ParkingOption]:
        if max_results <= 0:
            return []
        rng = random.Random(f"parking|{location.lat:.4f},{location.lng:.4f}")
        options = []
        for name, kind in rng.sample(sample.PARKING_SPOTS, min(max_results, 4)):
            point = _offset(location, rng.uniform(0.1, 0.95) * radius_m, rng.uniform(0, math.tau))
            options.append(
                ParkingOption(
                    name=f"[Sample] {name}",
                    address=_mock_address(rng, point),
                    location=point,
                    distance_m=round(haversine_m(location, point)),
                    kind=kind,
                    rating=round(rng.uniform(3.2, 4.7), 1),
                )
            )
        return sorted(options, key=lambda o: o.distance_m or 0)


# --------------------------------------------------------------------------- factory


def build_places_provider(settings: Settings) -> PlacesProvider:
    """Pick the provider from `settings.places_provider` ("auto" = Google when a Maps key is set, else mock)."""
    choice = settings.places_provider
    key = settings.maps_api_key
    if choice == "mock" or (choice == "auto" and not key):
        if choice == "auto":
            log.warning("No Google Maps key configured; venue search uses labelled sample data (MockPlacesProvider)")
        return MockPlacesProvider()
    if not key:
        raise ProviderError(
            "PLACES_PROVIDER=google but no key is set; set GOOGLE_MAPS_API_KEY (or GOOGLE_API_KEY) in .env, "
            "or use PLACES_PROVIDER=mock for sample data."
        )
    return GooglePlacesProvider(key)
