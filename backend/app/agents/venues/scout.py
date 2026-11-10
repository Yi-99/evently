"""Scout agent: turns the planner's criteria into a shortlist of real places to research.

Candidates only ever come from the places provider. When it fails or finds
nothing the scout fails with a clear message; it never invents venues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.venues import prompts
from app.agents.venues.models import SearchVenuesIn, SelectVenuesIn
from app.llm.base import Tool, ToolError
from app.providers.base import GeoPoint, PlaceCandidate, ProviderError
from app.schemas.venue import VenueCriteria

if TYPE_CHECKING:
    from app.agents.runtime import RunContext

RESULTS_PER_SEARCH = 8
SCOUT_MAX_TURNS = 8


class VenueSearchError(Exception):
    """The search cannot continue; the message is meant for the planner."""


def default_query(criteria: VenueCriteria) -> str:
    query = f"{criteria.event_type} venue" if criteria.event_type else "event venue"
    if criteria.headcount:
        query += f" for {criteria.headcount} people"
    if criteria.setting != "either":
        query += f" {criteria.setting}"
    return query


class Scout:
    """Search state for one scout run: everything found so far, and what was picked."""

    def __init__(self, run: RunContext, criteria: VenueCriteria, centre: GeoPoint | None) -> None:
        self.run = run
        self.ctx = run.ctx
        self.criteria = criteria
        self.centre = centre
        self.found: dict[str, PlaceCandidate] = {}
        self.queries: list[str] = []
        self.selected: list[str] | None = None
        self.last_error: str | None = None

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "search_venues",
                "Search for venues around the planner's location. Returns places with ids, address, type, "
                "rating and price level. Run it more than once with different wording to widen the pool.",
                SearchVenuesIn,
                self.search_venues,
            ),
            Tool(
                "select_venues",
                "Choose the venues to research, best fit first, by place id from the search results. "
                "Call it once; the list is capped at the planner's maximum.",
                SelectVenuesIn,
                self.select_venues,
            ),
        ]

    async def search_venues(self, args: SearchVenuesIn) -> list[dict[str, Any]]:
        self.queries.append(args.query)
        try:
            results = await self.ctx.places.search_venues(
                args.query,
                near=self.criteria.location,
                location=self.centre,
                radius_m=int(self.criteria.radius_km * 1000),
                max_results=RESULTS_PER_SEARCH,
            )
        except ProviderError as exc:
            self.last_error = str(exc)
            raise ToolError(f"The place search failed: {exc}") from exc
        for candidate in results:
            self.found.setdefault(candidate.place_id, candidate)
        await self.run.step(f"Searched '{args.query}': {len(results)} result(s), {len(self.found)} unique so far")
        return [
            candidate.model_dump(mode="json", exclude_none=True, exclude={"photo_urls", "location"})
            for candidate in results
        ]

    async def select_venues(self, args: SelectVenuesIn) -> str:
        unknown = [pid for pid in args.place_ids if pid not in self.found]
        if unknown:
            raise ToolError(f"Unknown place ids (only ids from search results can be selected): {unknown}")
        picks: list[str] = []
        for pid in args.place_ids:
            if pid not in picks:
                picks.append(pid)
        self.selected = picks[: self.criteria.max_candidates]
        return f"Selected {len(self.selected)} venue(s): {[self.found[p].name for p in self.selected]}"

    async def candidates(self) -> list[PlaceCandidate]:
        """The final shortlist; falls back to the provider's own ranking when the model did not choose."""
        if self.selected:
            return [self.found[pid] for pid in self.selected]
        if not self.found and not self.queries:
            await self.search_venues(SearchVenuesIn(query=default_query(self.criteria)))
        if not self.found:
            if self.last_error:
                raise VenueSearchError(f"Venue lookup failed: {self.last_error}")
            raise VenueSearchError(
                f"No venues found within {self.criteria.radius_km:g} km of {self.criteria.location}. "
                "Try a wider radius or a different area."
            )
        return list(self.found.values())[: self.criteria.max_candidates]


def make_scout(criteria: VenueCriteria):
    """Agent function for the `venue_scout` run. Output: `{candidates, centre, queries}`."""

    async def scout(run: RunContext) -> dict[str, Any]:
        await run.step(f"Locating {criteria.location}", progress=0.1)
        try:
            centre = await run.ctx.places.geocode(criteria.location)
        except ProviderError as exc:
            raise VenueSearchError(f"Could not look up '{criteria.location}': {exc}") from exc

        state = Scout(run, criteria, centre)
        await run.step("Searching for venues", progress=0.3)
        result = await run.ctx.llm.run(
            purpose="venue_scout",
            system=prompts.SCOUT_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": prompts.scout_prompt(
                        criteria.model_dump(mode="json"),
                        centre_found=centre is not None,
                        max_candidates=criteria.max_candidates,
                    ),
                }
            ],
            tools=state.tools(),
            max_turns=SCOUT_MAX_TURNS,
        )
        run.add_usage(result.usage)
        candidates = await state.candidates()
        await run.step(f"Shortlisted {len(candidates)} venue(s)", progress=0.9)
        return {
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "centre": centre.model_dump() if centre else None,
            "queries": state.queries,
        }

    return scout
