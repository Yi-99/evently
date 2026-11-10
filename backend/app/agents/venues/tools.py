"""The tools a venue research (or refine) agent works with, bound to one card.

Lookups write what they measured to the card the moment they return, so the
whiteboard fills in live; the final submission then layers the model's judgement
on top. Measured facts always win over the model's numbers for the same field.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import TYPE_CHECKING, Any

from app.agents.venues import prompts
from app.agents.venues.cards import CardWriter, place_location, place_summary
from app.agents.venues.models import (
    CardUpdate,
    DriveTimeIn,
    FindParkingIn,
    NoInput,
    RefineUpdate,
    VenueAssessment,
    WeatherIn,
)
from app.llm.base import LLMError, Tool, ToolError
from app.providers.base import GeoPoint
from app.schemas.venue import ParkingSpot, PlaceSummary, Source, VenueCriteria

if TYPE_CHECKING:
    from app.agents.runtime import RunContext

WEB_SEARCH_MAX_USES = 4
MAX_TURNS = 16
REMINDER_TURNS = 4
MILESTONES = ("details", "parking", "weather", "traffic", "saved")


class ResearchIncomplete(LLMError):
    """The agent finished its loop without saving anything to the card."""


def web_search_tools(ctx: Any) -> list[dict[str, Any]] | None:
    if not ctx.settings.llm_web_search:
        return None
    return [{"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}]


def _hour_window(start: time | None, end: time | None) -> tuple[int | None, int | None]:
    start_hour = start.hour if start else None
    end_hour = None
    if end is not None:
        end_hour = min(23, end.hour + (1 if (end.minute or end.second) else 0))
        if start_hour is not None and end_hour <= start_hour:
            end_hour = 23  # the event runs past midnight; look at the rest of the day
    return start_hour, end_hour


def _minutes(value: float | None) -> str:
    return "?" if value is None else f"{round(value)} min"


class ResearchToolkit:
    """Tools and card state for one agent on one venue card."""

    def __init__(
        self,
        run: RunContext,
        *,
        card_id: str,
        place: PlaceSummary,
        criteria: VenueCriteria,
        centre: GeoPoint | None = None,
    ) -> None:
        self.run = run
        self.ctx = run.ctx
        self.card = CardWriter(run.ctx, card_id)
        self.place = place
        self.criteria = criteria
        self.centre = centre
        self.facts: dict[str, dict[str, Any]] = {}  # measured values per section; they outrank the model's
        self.sources: list[Source] = []
        self.saved = False
        self.last_update: CardUpdate | None = None
        self._done: set[str] = set()
        self._parking_features: list[str] = []
        # Tool calls of one turn run concurrently; run steps and card writes are read-modify-write.
        self._lock = asyncio.Lock()

    # -- tool definitions -------------------------------------------------------

    def lookup_tools(self) -> list[Tool]:
        return [
            Tool(
                "get_place_details",
                "Full listing for this venue: contact details, opening hours, editorial summary, review "
                "snippets, parking and accessibility features, and a stated capacity when the source has one.",
                NoInput,
                self.get_place_details,
            ),
            Tool(
                "find_parking",
                "Parking lots, garages and street parking near the venue, with distances. Combine with the "
                "venue's own parking features from get_place_details.",
                FindParkingIn,
                self.find_parking,
            ),
            Tool(
                "get_weather_outlook",
                "Weather for the event date at the venue: a forecast when the date is close enough, otherwise "
                "climatology for that time of year (the result says which). Covers the event's hours when known.",
                WeatherIn,
                self.get_weather_outlook,
            ),
            Tool(
                "get_drive_time",
                "Drive time to the venue: typical, and arriving for the event start with predicted traffic, "
                "plus whether that overlaps rush hour. The result says whether it is a routing or a heuristic "
                "estimate.",
                DriveTimeIn,
                self.get_drive_time,
            ),
        ]

    def submit_tool(self) -> Tool:
        return Tool(
            "submit_assessment",
            "Save your final assessment to the card. Required: capacity, pros, cons, score, summary. Include "
            "the other sections when you have a judgement to add; measured figures already on the card are kept.",
            VenueAssessment,
            self.submit_assessment,
        )

    def update_tool(self) -> Tool:
        return Tool(
            "update_card",
            "Save your changes to the card. Include only the sections the instruction affected; omitted "
            "sections and omitted fields keep their current values. Always include `note`.",
            RefineUpdate,
            self.update_card,
        )

    # -- lookups -----------------------------------------------------------------

    async def get_place_details(self, _: NoInput) -> dict[str, Any]:
        if not self.place.place_id:
            raise ToolError("This venue has no place id, so no listing can be fetched.")
        details = await self.ctx.places.get_details(self.place.place_id)
        if details is None:
            raise ToolError("The places provider has no listing for this venue; work from the card's data.")
        self.place = place_summary(details, base=self.place)
        self._parking_features = list(details.parking_features)
        sections: dict[str, dict[str, Any]] = {"place": self.place.model_dump(exclude_none=True)}
        if details.capacity_hint:
            sections["capacity"] = {
                "estimated_capacity": details.capacity_hint,
                "source": f"Stated in the {self.ctx.places.name} listing",
            }
        self.sources.append(
            Source(title=f"{self.place.name} listing ({self.ctx.places.name})", url=details.maps_url or details.website)
        )
        await self._record("details", f"Loaded listing for {self.place.name}", sections)
        return details.model_dump(mode="json", exclude_none=True, exclude={"photo_urls"})

    async def find_parking(self, args: FindParkingIn) -> dict[str, Any]:
        location = await self._location()
        options = await self.ctx.places.find_parking(location, radius_m=args.radius_m)
        spots = [
            ParkingSpot(name=o.name, distance_m=o.distance_m, address=o.address, kind=o.kind).model_dump(mode="json")
            for o in options
        ]
        self.facts["parking"] = {"nearby": spots}
        message = (
            f"Parking: {len(spots)} option(s) within {args.radius_m} m"
            if spots
            else f"Parking: nothing found within {args.radius_m} m"
        )
        await self._record("parking", message, {"parking": self.facts["parking"]})
        return {"venue_parking_features": self._parking_features, "nearby": spots}

    async def get_weather_outlook(self, args: WeatherIn) -> dict[str, Any]:
        day = args.event_date or self.criteria.event_date
        if day is None:
            raise ToolError("No event date is set, so there is no weather to look up. Record weather as unknown.")
        location = await self._location()
        start_hour, end_hour = _hour_window(
            args.start_time or self.criteria.start_time, args.end_time or self.criteria.end_time
        )
        outlook = await self.ctx.weather.get_outlook(location, day, start_hour=start_hour, end_hour=end_hour)
        if outlook is None:
            raise ToolError("No weather data is available for that date and place. Record weather as unknown.")
        self.facts["weather"] = {
            "temp_high_c": outlook.temp_high_c,
            "temp_low_c": outlook.temp_low_c,
            "precipitation_probability": outlook.precipitation_probability,
            "wind_kph": outlook.wind_kph,
            "basis": outlook.basis,
        }
        summary = outlook.window_summary or outlook.summary
        self.sources.append(Source(title=f"Weather {outlook.basis} for {day.isoformat()} ({self.ctx.weather.name})"))
        section = {**self.facts["weather"], "summary": summary}
        await self._record("weather", f"Weather ({outlook.basis}): {summary}", {"weather": section})
        return outlook.model_dump(mode="json", exclude_none=True)

    async def get_drive_time(self, args: DriveTimeIn) -> dict[str, Any]:
        destination = await self._location()
        origin_label, origin = await self._origin(args.origin)
        day = args.event_date or self.criteria.event_date
        start = args.start_time or self.criteria.start_time
        arrive_by = datetime.combine(day, start) if day and start else None
        estimate = await self.ctx.traffic.estimate_travel(origin, destination, arrive_by=arrive_by)
        if estimate is None:
            raise ToolError("No route could be estimated from that origin. Record traffic as unknown.")
        facts: dict[str, Any] = {"origin": origin_label, "typical_minutes": estimate.typical_minutes}
        if arrive_by is not None:
            facts.update(
                event_time_minutes=estimate.event_time_minutes,
                delay_ratio=estimate.delay_ratio,
                rush_hour_overlap=estimate.rush_hour_overlap,
            )
            summary = (
                f"About {_minutes(estimate.event_time_minutes)} from {origin_label} arriving for the event start "
                f"({_minutes(estimate.typical_minutes)} typical)"
            )
        else:
            # Without a start time there is no arrival to predict traffic for; do not pretend otherwise.
            summary = (
                f"About {_minutes(estimate.typical_minutes)} from {origin_label} without traffic; no start time set"
            )
        if estimate.basis == "heuristic":
            summary += " (heuristic estimate)"
        self.facts["traffic"] = facts
        self.sources.append(Source(title=f"Drive-time {estimate.basis.replace('_', ' ')} ({self.ctx.traffic.name})"))
        await self._record("traffic", f"Traffic: {summary}", {"traffic": {**facts, "summary": summary}})
        result = estimate.model_dump(mode="json", exclude_none=True)
        if arrive_by is None:
            result["note"] = "No event start time is set, so event-time traffic was not estimated."
        return result

    # -- saving --------------------------------------------------------------------

    async def submit_assessment(self, update: VenueAssessment) -> str:
        skipped = await self._apply(update)
        return "Assessment saved. " + self._skipped_note(skipped) + "Reply with one short closing sentence."

    async def update_card(self, update: RefineUpdate) -> str:
        skipped = await self._apply(update)
        return "Card updated. " + self._skipped_note(skipped) + "Reply with one short closing sentence."

    async def _apply(self, update: CardUpdate) -> list[str]:
        sections: dict[str, dict[str, Any]] = {}
        for key in ("capacity", "parking", "weather", "traffic", "pricing"):
            verdict = getattr(update, key)
            if verdict is not None:
                sections[key] = {**verdict.model_dump(exclude_none=True), **self.facts.get(key, {})}
        changes = getattr(update, "criteria_changes", None)
        if changes is not None and changes.model_fields_set:
            sections["criteria"] = changes.model_dump(exclude_none=True)
        data: dict[str, Any] = {}
        for key in ("pros", "cons", "summary"):
            value = getattr(update, key)
            if value is not None:
                data[key] = value
        if update.score is not None:
            data["score"] = update.score.model_dump(mode="json")
        async with self._lock:
            skipped = await self.card.write(
                sections=sections, data=data, add_sources=[*(update.sources or []), *self.sources]
            )
            self.saved = True
            self.last_update = update
            self._done.add("saved")
            await self.run.step("Assessment saved to the card", progress=self._progress())
        return skipped

    @staticmethod
    def _skipped_note(skipped: list[str]) -> str:
        if not skipped:
            return ""
        return f"Not changed because the planner locked them: {', '.join(skipped)}. "

    # -- helpers -------------------------------------------------------------------

    async def _location(self) -> GeoPoint:
        location = place_location(self.place)
        if location is None and self.place.address:
            location = await self.ctx.places.geocode(self.place.address)
            if location is not None:
                self.place = self.place.model_copy(update={"lat": location.lat, "lng": location.lng})
        if location is None:
            raise ToolError("No coordinates are known for this venue, so this lookup is not possible.")
        return location

    async def _origin(self, override: str | None) -> tuple[str, GeoPoint | str]:
        """Label and provider argument for where attendees set off from."""
        named = override or self.criteria.attendee_origin
        if named:
            return named, named
        if self.centre is None:
            self.centre = await self.ctx.places.geocode(self.criteria.location)
        label = f"{self.criteria.location} (centre)"
        return label, self.centre or self.criteria.location

    def _progress(self) -> float:
        return round(0.1 + 0.85 * len(self._done) / len(MILESTONES), 2)

    async def _record(self, milestone: str, message: str, sections: dict[str, dict[str, Any]]) -> None:
        """Write measured facts to the card and report progress, atomically."""
        async with self._lock:
            await self.card.write(sections=sections)
            self._done.add(milestone)
            await self.run.step(message, progress=self._progress())


def with_user_note(messages: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Append `text` as the user's next turn, merging into a trailing tool-result message if there is one."""
    if messages and messages[-1].get("role") == "user":
        last = messages[-1]
        content = last["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        return [*messages[:-1], {**last, "content": [*blocks, {"type": "text", "text": text}]}]
    return [*messages, {"role": "user", "content": text}]


async def run_agent(
    kit: ResearchToolkit,
    *,
    purpose: str,
    system: str,
    prompt: str,
    tools: list[Tool],
    final_tool: str,
) -> None:
    """Run the tool loop until the agent has saved to the card, with one reminder if it stops early.

    Raises `ResearchIncomplete` when it still has not saved after the reminder.
    """
    ctx = kit.ctx
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    result = await ctx.llm.run(
        purpose=purpose,
        system=system,
        messages=messages,
        tools=tools,
        server_tools=web_search_tools(ctx),
        max_turns=MAX_TURNS,
    )
    kit.run.add_usage(result.usage)
    if not kit.saved:
        # A tool call cannot be forced, so a model that ends early gets one explicit reminder.
        messages = with_user_note([*messages, *result.new_messages], prompts.REMINDER.format(tool=final_tool))
        result = await ctx.llm.run(
            purpose=purpose,
            system=system,
            messages=messages,
            tools=tools,
            server_tools=web_search_tools(ctx),
            max_turns=REMINDER_TURNS,
        )
        kit.run.add_usage(result.usage)
    if not kit.saved:
        raise ResearchIncomplete(f"The agent finished without calling {final_tool}.")
