"""Venue search criteria and the shape of a venue card's `data`.

`VenueCardData` is the contract between the research agents (who fill it in
section by section) and the frontend (which renders it).
"""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, Field

Fit = Literal["yes", "tight", "no", "unknown"]
Risk = Literal["low", "medium", "high", "unknown"]


class VenueCriteria(BaseModel):
    """What the planner is looking for. Also stored as `Event.requirements`."""

    location: str = Field(description="City, neighbourhood or address to search around.")
    headcount: int | None = Field(default=None, ge=1, description="Expected number of attendees.")
    radius_km: float = Field(default=15, gt=0, le=200)
    event_date: date | None = None
    start_time: time | None = Field(default=None, description="Local start time at the venue.")
    end_time: time | None = None
    event_type: str | None = Field(default=None, description="e.g. conference, wedding, offsite.")
    setting: Literal["indoor", "outdoor", "either"] = "either"
    budget: str | None = Field(default=None, description="Free text, e.g. 'under $10k'.")
    parking_required: bool = True
    attendee_origin: str | None = Field(
        default=None, description="Where most attendees travel from; used for rush-hour estimates."
    )
    must_haves: list[str] = Field(default_factory=list)
    notes: str | None = None
    max_candidates: int = Field(default=5, ge=1, le=12)


class PlaceSummary(BaseModel):
    name: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    place_id: str | None = None
    website: str | None = None
    phone: str | None = None
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    types: list[str] = Field(default_factory=list)
    photo_urls: list[str] = Field(default_factory=list)
    maps_url: str | None = None


class CapacityAssessment(BaseModel):
    estimated_capacity: int | None = None
    fits_headcount: Fit = "unknown"
    notes: str | None = None
    source: str | None = None


class ParkingSpot(BaseModel):
    name: str
    distance_m: int | None = None
    address: str | None = None
    kind: str | None = None  # lot | garage | street | valet
    notes: str | None = None


class ParkingAssessment(BaseModel):
    on_site: bool | None = None
    summary: str | None = None
    nearby: list[ParkingSpot] = Field(default_factory=list)
    score: int | None = Field(default=None, ge=0, le=100)


class WeatherAssessment(BaseModel):
    summary: str | None = None
    temp_high_c: float | None = None
    temp_low_c: float | None = None
    precipitation_probability: float | None = Field(default=None, ge=0, le=1)
    wind_kph: float | None = None
    basis: Literal["forecast", "climatology", "unknown"] = "unknown"
    risk: Risk = "unknown"
    notes: str | None = None  # e.g. "outdoor terrace needs a rain plan"


class TrafficAssessment(BaseModel):
    summary: str | None = None
    origin: str | None = None
    typical_minutes: float | None = None  # free-flow drive time
    event_time_minutes: float | None = None  # drive time arriving for the event start
    delay_ratio: float | None = None  # event_time / typical
    rush_hour_overlap: bool | None = None
    transit_notes: str | None = None
    score: int | None = Field(default=None, ge=0, le=100)


class PricingAssessment(BaseModel):
    summary: str | None = None
    estimate: str | None = None
    within_budget: Fit = "unknown"


class VenueScore(BaseModel):
    overall: int = Field(ge=0, le=100)
    capacity: int | None = Field(default=None, ge=0, le=100)
    parking: int | None = Field(default=None, ge=0, le=100)
    weather: int | None = Field(default=None, ge=0, le=100)
    traffic: int | None = Field(default=None, ge=0, le=100)
    value: int | None = Field(default=None, ge=0, le=100)


class Source(BaseModel):
    title: str
    url: str | None = None


class VenueCardData(BaseModel):
    """`Card.data` when `Card.kind == "venue"`. Sections are None until researched."""

    place: PlaceSummary
    criteria: VenueCriteria | None = None
    capacity: CapacityAssessment | None = None
    parking: ParkingAssessment | None = None
    weather: WeatherAssessment | None = None
    traffic: TrafficAssessment | None = None
    pricing: PricingAssessment | None = None
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    score: VenueScore | None = None
    summary: str | None = None
    sources: list[Source] = Field(default_factory=list)
    user_notes: str | None = None
    rank: int | None = None
