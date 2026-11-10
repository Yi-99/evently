"""Data-provider contracts for venue research (places, weather, traffic).

Agents reach providers through `ctx.places`, `ctx.weather` and `ctx.traffic`.
Implementations must not raise for ordinary upstream failures on lookups that
have a sensible empty answer (return `[]` / `None`); raise `ProviderError` when
the caller needs to know the lookup itself failed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """An upstream data provider failed or is misconfigured."""


class GeoPoint(BaseModel):
    lat: float
    lng: float


class PlaceCandidate(BaseModel):
    place_id: str
    name: str
    address: str | None = None
    location: GeoPoint | None = None
    types: list[str] = Field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    website: str | None = None
    maps_url: str | None = None
    photo_urls: list[str] = Field(default_factory=list)


class PlaceDetails(PlaceCandidate):
    phone: str | None = None
    opening_hours: list[str] = Field(default_factory=list)  # human-readable, one per weekday
    editorial_summary: str | None = None
    review_snippets: list[str] = Field(default_factory=list)
    # Parking facts the provider reports for the venue itself (e.g. Google `parkingOptions`).
    parking_features: list[str] = Field(default_factory=list)
    accessibility_features: list[str] = Field(default_factory=list)
    capacity_hint: int | None = None  # only when the source states one


class ParkingOption(BaseModel):
    name: str
    address: str | None = None
    location: GeoPoint | None = None
    distance_m: int | None = None
    kind: str | None = None  # lot | garage | street | valet
    rating: float | None = None


class WeatherOutlook(BaseModel):
    day: date
    basis: Literal["forecast", "climatology"]
    summary: str
    temp_high_c: float | None = None
    temp_low_c: float | None = None
    precipitation_probability: float | None = Field(default=None, ge=0, le=1)
    precipitation_mm: float | None = None
    wind_kph: float | None = None
    # Present when the caller asked about a specific window of the day.
    window_summary: str | None = None


class TravelEstimate(BaseModel):
    origin: str
    typical_minutes: float | None = None  # without traffic
    event_time_minutes: float | None = None  # with predicted traffic for the requested time
    distance_km: float | None = None
    delay_ratio: float | None = None  # event_time / typical
    rush_hour_overlap: bool = False
    basis: Literal["routes_api", "heuristic"] = "heuristic"
    notes: str | None = None


class PlacesProvider(Protocol):
    name: str

    async def geocode(self, address: str) -> GeoPoint | None: ...

    async def search_venues(
        self,
        query: str,
        *,
        near: str | None = None,
        location: GeoPoint | None = None,
        radius_m: int = 15000,
        max_results: int = 8,
    ) -> list[PlaceCandidate]:
        """Text search for venues, e.g. query="conference venue for 200 people", near="Austin, TX"."""
        ...

    async def get_details(self, place_id: str) -> PlaceDetails | None: ...

    async def find_parking(
        self, location: GeoPoint, *, radius_m: int = 800, max_results: int = 5
    ) -> list[ParkingOption]: ...


class WeatherProvider(Protocol):
    name: str

    async def get_outlook(
        self, location: GeoPoint, day: date, *, start_hour: int | None = None, end_hour: int | None = None
    ) -> WeatherOutlook | None:
        """Forecast when `day` is within forecast range, otherwise climatology for that time of year."""
        ...


class TrafficProvider(Protocol):
    name: str

    async def estimate_travel(
        self, origin: GeoPoint | str, destination: GeoPoint, *, arrive_by: datetime | None = None
    ) -> TravelEstimate | None:
        """Drive time to the venue, typical vs. arriving at `arrive_by` (venue-local, tz-aware or naive)."""
        ...
