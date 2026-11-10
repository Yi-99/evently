"""Pydantic models private to the venue agents: tool inputs and structured answers.

Card sections themselves are the models in `app.schemas.venue`; the models here
wrap them so the LLM's submissions are validated before anything reaches a card.
"""

from __future__ import annotations

from datetime import date, time

from pydantic import BaseModel, Field

from app.schemas.venue import (
    CapacityAssessment,
    ParkingAssessment,
    PricingAssessment,
    Source,
    TrafficAssessment,
    VenueScore,
    WeatherAssessment,
)

# --- scout -------------------------------------------------------------------


class SearchVenuesIn(BaseModel):
    query: str = Field(description="Free-text venue search, e.g. 'rooftop event space for 150 guests'.")


class SelectVenuesIn(BaseModel):
    place_ids: list[str] = Field(
        min_length=1, description="Place ids from the search results, best fit first. Each one becomes a card."
    )


# --- research tools -----------------------------------------------------------


class NoInput(BaseModel):
    pass


class FindParkingIn(BaseModel):
    radius_m: int = Field(default=800, ge=100, le=3000, description="Search radius around the venue in metres.")


class WeatherIn(BaseModel):
    event_date: date | None = Field(
        default=None, description="Defaults to the planner's event date. Set only to check a different day."
    )
    start_time: time | None = Field(default=None, description="Defaults to the planner's start time.")
    end_time: time | None = Field(default=None, description="Defaults to the planner's end time.")


class DriveTimeIn(BaseModel):
    origin: str | None = Field(
        default=None,
        description="Where attendees set off from. Defaults to the planner's attendee origin, else the city centre.",
    )
    event_date: date | None = Field(default=None, description="Defaults to the planner's event date.")
    start_time: time | None = Field(default=None, description="Arrival time; defaults to the planner's start time.")


class CardUpdate(BaseModel):
    """Partial update of a venue card. Omitted sections keep their current content."""

    capacity: CapacityAssessment | None = None
    parking: ParkingAssessment | None = None
    weather: WeatherAssessment | None = None
    traffic: TrafficAssessment | None = None
    pricing: PricingAssessment | None = None
    pros: list[str] | None = None
    cons: list[str] | None = None
    score: VenueScore | None = None
    summary: str | None = Field(default=None, description="Two to four plain-language sentences for the planner.")
    sources: list[Source] | None = Field(
        default=None, description="Where the facts came from (venue site, listings, articles). Added to the card."
    )


class VenueAssessment(CardUpdate):
    """The research agent's final word on one venue."""

    capacity: CapacityAssessment
    pros: list[str]
    cons: list[str]
    score: VenueScore
    summary: str = Field(description="Two to four plain-language sentences for the planner.")


class CriteriaChanges(BaseModel):
    """Assumptions the planner changed for this card only."""

    headcount: int | None = Field(default=None, ge=1)
    event_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    attendee_origin: str | None = None


class RefineUpdate(CardUpdate):
    note: str = Field(description="One or two sentences telling the planner what you found and what changed.")
    criteria_changes: CriteriaChanges | None = Field(
        default=None, description="Only when the instruction changed an assumption (headcount, date, times, origin)."
    )


# --- coordinator --------------------------------------------------------------


class RankedOneLiner(BaseModel):
    rank: int
    one_liner: str = Field(description="At most 15 words: the single thing the planner should know about it.")


class SearchSummary(BaseModel):
    summary: str = Field(description="Three to five plain sentences comparing the venues and naming a top pick.")
    one_liners: list[RankedOneLiner]
