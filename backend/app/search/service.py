"""Search queries over the Elasticsearch indices.

The `build_*_query` functions are pure (no I/O) and return the keyword arguments for
`AsyncElasticsearch.search`, so they can be inspected and tested without a cluster. The `search_*`
coroutines run them and shape the hits for the API; `search_venue_memory` is the compact variant meant
for the chat orchestrator's tool.

Ranking
-------
* Venues: a `bool` query whose `should` clauses are a BM25 `multi_match` over the venue text fields
  and, when the index has a `semantic_text` field, a `semantic` query over the natural-language blob
  (`semantic` mode). Scores are summed, which is a simple hybrid that works on the free licence tier;
  switch to an `rrf` retriever if the cluster is licensed for it. Filters (owner, geo distance, capacity,
  parking, score, event type) never affect the score. Without `q` the results are ordered by overall
  score (and distance when `near` is given).
* Event content and session transcripts: BM25 `multi_match` with fuzziness, highlighted.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Literal

from elasticsearch.exceptions import ApiError, TransportError
from sqlalchemy import select

from app.models import Event
from app.search.client import SearchClient, SearchUnavailable, get_search_client, require_search_client
from app.search.indices import VENUE_SEMANTIC_FIELD, VENUE_TEXT_FIELDS

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

ContentType = Literal["transcript", "message", "slide"]
CONTENT_TYPES: tuple[ContentType, ...] = ("transcript", "message", "slide")

# "Has parking" means on-site parking or a parking score at least this high.
PARKING_MIN_SCORE = 60
MAX_LIMIT = 100
HIGHLIGHT_TAGS = {"pre_tags": ["<em>"], "post_tags": ["</em>"]}

_VENUE_HIGHLIGHT_FIELDS = (
    "name",
    "summary",
    "pros",
    "cons",
    "parking_summary",
    "weather_summary",
    "traffic_summary",
    "user_notes",
    "address",
)
_VENUE_SOURCE_FIELDS = [
    "card_id",
    "event_id",
    "event_name",
    "owner_id",
    "title",
    "status",
    "name",
    "address",
    "place_id",
    "website",
    "maps_url",
    "location",
    "types",
    "rating",
    "review_count",
    "price_level",
    "capacity_estimate",
    "capacity_fit",
    "parking_on_site",
    "parking_summary",
    "parking_score",
    "weather_summary",
    "weather_risk",
    "traffic_summary",
    "traffic_score",
    "pricing_estimate",
    "score_overall",
    "pros",
    "cons",
    "summary",
    "criteria_headcount",
    "criteria_event_type",
    "criteria_event_date",
    "criteria_location",
    "updated_at",
]


def parse_near(value: str | None) -> tuple[float, float] | None:
    """`"lat,lng"` -> (lat, lng); raises ValueError on malformed input."""
    if not value:
        return None
    try:
        lat_s, lng_s = value.split(",", 1)
        lat, lng = float(lat_s.strip()), float(lng_s.strip())
    except ValueError:
        raise ValueError("near must be 'lat,lng'") from None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("near is out of range")
    return lat, lng


def _clamp_limit(limit: int | None, default: int = 20) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIMIT))


# --- query builders -------------------------------------------------------------


def build_venue_query(
    *,
    q: str | None,
    owner_id: str | None,
    near: tuple[float, float] | None = None,
    radius_km: float | None = None,
    min_capacity: int | None = None,
    parking: bool | None = None,
    min_score: int | None = None,
    event_type: str | None = None,
    exclude_event_id: str | None = None,
    semantic: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Keyword arguments for `es.search` against the venues index."""
    should: list[dict[str, Any]] = []
    if q:
        should.append(
            {
                "multi_match": {
                    "query": q,
                    "fields": VENUE_TEXT_FIELDS,
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                }
            }
        )
        if semantic:
            should.append({"semantic": {"field": VENUE_SEMANTIC_FIELD, "query": q}})

    filters: list[dict[str, Any]] = []
    if owner_id:
        filters.append({"term": {"owner_id": owner_id}})
    if near is not None:
        filters.append(
            {
                "geo_distance": {
                    "distance": f"{radius_km or 25}km",
                    "location": {"lat": near[0], "lon": near[1]},
                }
            }
        )
    if min_capacity:
        filters.append({"range": {"capacity_estimate": {"gte": int(min_capacity)}}})
    if parking:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"parking_on_site": True}},
                        {"range": {"parking_score": {"gte": PARKING_MIN_SCORE}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if min_score is not None:
        filters.append({"range": {"score_overall": {"gte": int(min_score)}}})
    if event_type:
        filters.append({"match": {"criteria_event_type": {"query": event_type, "operator": "and"}}})

    must_not: list[dict[str, Any]] = []
    if exclude_event_id:
        must_not.append({"term": {"event_id": exclude_event_id}})

    query: dict[str, Any] = {"bool": {"filter": filters, "must_not": must_not}}
    if should:
        query["bool"]["should"] = should
        query["bool"]["minimum_should_match"] = 1

    sort: list[Any] = []
    if q:
        sort.append({"_score": {"order": "desc"}})
    else:
        sort.append({"score_overall": {"order": "desc", "missing": "_last"}})
    if near is not None:
        sort.append(
            {
                "_geo_distance": {
                    "location": {"lat": near[0], "lon": near[1]},
                    "order": "asc",
                    "unit": "km",
                    "ignore_unmapped": True,
                }
            }
        )
    sort.append({"updated_at": {"order": "desc", "missing": "_last"}})

    body: dict[str, Any] = {
        "query": query,
        "size": _clamp_limit(limit),
        "sort": sort,
        "source": _VENUE_SOURCE_FIELDS,
        "track_total_hits": True,
    }
    if q:
        body["highlight"] = {
            "fields": {f: {} for f in _VENUE_HIGHLIGHT_FIELDS},
            "fragment_size": 160,
            "number_of_fragments": 2,
            **HIGHLIGHT_TAGS,
        }
    return body


def build_event_content_query(*, q: str, event_id: str, limit: int = 20) -> dict[str, Any]:
    """Keyword arguments for `es.search` across transcripts, messages and slides of one event."""
    return {
        "query": {
            "bool": {
                "filter": [{"term": {"event_id": event_id}}],
                "must": [
                    {
                        "multi_match": {
                            "query": q,
                            "fields": ["text", "content", "title^2", "notes", "keywords^2", "outline_text"],
                            "type": "best_fields",
                            "fuzziness": "AUTO",
                            "prefix_length": 1,
                        }
                    }
                ],
            }
        },
        "size": _clamp_limit(limit),
        "highlight": {
            "fields": {
                "text": {},
                "content": {},
                "title": {},
                "notes": {},
                "keywords": {},
                "outline_text": {},
            },
            "fragment_size": 160,
            "number_of_fragments": 2,
            **HIGHLIGHT_TAGS,
        },
        "track_total_hits": True,
    }


def build_session_transcript_query(*, q: str, session_id: str, limit: int = 20) -> dict[str, Any]:
    """Keyword arguments for `es.search` over one live session's transcript."""
    return {
        "query": {
            "bool": {
                "filter": [{"term": {"session_id": session_id}}],
                "must": [{"match": {"text": {"query": q, "fuzziness": "AUTO", "prefix_length": 1, "operator": "or"}}}],
            }
        },
        "size": _clamp_limit(limit),
        "sort": [{"_score": {"order": "desc"}}, {"seq": {"order": "asc"}}],
        "highlight": {"fields": {"text": {}}, "fragment_size": 200, "number_of_fragments": 1, **HIGHLIGHT_TAGS},
        "track_total_hits": True,
    }


# --- hit shaping ----------------------------------------------------------------


def _venue_hit(hit: dict[str, Any], *, has_near: bool, has_q: bool) -> dict[str, Any]:
    src = hit.get("_source") or {}
    sort_values = hit.get("sort") or []
    distance_km: float | None = None
    if has_near and sort_values:
        # sort = [_score | score_overall, _geo_distance, updated_at] -> distance is second
        try:
            value = float(sort_values[1])  # "Infinity" for documents without a location
            distance_km = round(value, 2) if math.isfinite(value) else None
        except (IndexError, TypeError, ValueError):
            distance_km = None
    location = src.get("location") or {}
    return {
        "card_id": src.get("card_id"),
        "event_id": src.get("event_id"),
        "event_name": src.get("event_name"),
        "name": src.get("name"),
        "address": src.get("address"),
        "place_id": src.get("place_id"),
        "website": src.get("website"),
        "maps_url": src.get("maps_url"),
        "lat": location.get("lat"),
        "lng": location.get("lon"),
        "distance_km": distance_km,
        "types": src.get("types") or [],
        "rating": src.get("rating"),
        "review_count": src.get("review_count"),
        "price_level": src.get("price_level"),
        "capacity_estimate": src.get("capacity_estimate"),
        "capacity_fit": src.get("capacity_fit"),
        "parking_on_site": src.get("parking_on_site"),
        "parking_summary": src.get("parking_summary"),
        "parking_score": src.get("parking_score"),
        "weather_summary": src.get("weather_summary"),
        "weather_risk": src.get("weather_risk"),
        "traffic_summary": src.get("traffic_summary"),
        "traffic_score": src.get("traffic_score"),
        "pricing_estimate": src.get("pricing_estimate"),
        "score_overall": src.get("score_overall"),
        "pros": src.get("pros") or [],
        "cons": src.get("cons") or [],
        "summary": src.get("summary"),
        "criteria": {
            "headcount": src.get("criteria_headcount"),
            "event_type": src.get("criteria_event_type"),
            "event_date": src.get("criteria_event_date"),
            "location": src.get("criteria_location"),
        },
        "status": src.get("status"),
        "updated_at": src.get("updated_at"),
        "relevance": hit.get("_score") if has_q else None,
        "highlights": hit.get("highlight") or {},
    }


def _content_type_for(index: str, names) -> ContentType | None:
    if index == names.transcripts:
        return "transcript"
    if index == names.messages:
        return "message"
    if index == names.slides:
        return "slide"
    return None


def _content_hit(hit: dict[str, Any], names) -> dict[str, Any] | None:
    src = hit.get("_source") or {}
    kind = _content_type_for(hit.get("_index", ""), names)
    if kind is None:
        return None
    highlights = hit.get("highlight") or {}
    base: dict[str, Any] = {
        "type": kind,
        "id": hit.get("_id"),
        "event_id": src.get("event_id"),
        "score": hit.get("_score"),
        "highlights": highlights,
    }
    if kind == "transcript":
        base.update(
            {
                "text": src.get("text"),
                "link": {
                    "session_id": src.get("session_id"),
                    "seq": src.get("seq"),
                    "segment_id": src.get("segment_id"),
                    "slide_index": src.get("slide_index"),
                },
                "speaker": src.get("speaker"),
                "start_ms": src.get("start_ms"),
                "end_ms": src.get("end_ms"),
                "created_at": src.get("created_at"),
            }
        )
    elif kind == "message":
        base.update(
            {
                "text": src.get("content"),
                "link": {"message_id": src.get("message_id")},
                "role": src.get("role"),
                "created_at": src.get("created_at"),
            }
        )
    else:
        base.update(
            {
                "text": src.get("notes") or src.get("outline_text"),
                "title": src.get("title"),
                "link": {
                    "slide_id": src.get("slide_id"),
                    "presentation_id": src.get("presentation_id"),
                    "position": src.get("position"),
                },
                "presentation_title": src.get("presentation_title"),
                "keywords": src.get("keywords") or [],
                "updated_at": src.get("updated_at"),
            }
        )
    return base


async def _run_search(client: SearchClient, index: str | list[str], body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.es.search(index=index, ignore_unavailable=True, **body)
    except TransportError as exc:
        client.mark_disconnected(exc)
        raise SearchUnavailable(f"Elasticsearch is not reachable: {exc.__class__.__name__}") from exc
    except ApiError as exc:
        raise SearchUnavailable(f"Elasticsearch rejected the search: {exc.message}") from exc
    return response.body


def _total(response: dict[str, Any]) -> int:
    total = (response.get("hits") or {}).get("total") or {}
    return int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)


# --- public search API ----------------------------------------------------------


async def search_venues(
    ctx: AppContext,
    *,
    owner_id: str,
    q: str | None = None,
    near: tuple[float, float] | None = None,
    radius_km: float | None = None,
    min_capacity: int | None = None,
    parking: bool | None = None,
    min_score: int | None = None,
    event_type: str | None = None,
    exclude_event_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Venue knowledge base search, scoped to the venues on the caller's events."""
    client = require_search_client(ctx)
    body = build_venue_query(
        q=q,
        owner_id=owner_id,
        near=near,
        radius_km=radius_km,
        min_capacity=min_capacity,
        parking=parking,
        min_score=min_score,
        event_type=event_type,
        exclude_event_id=exclude_event_id,
        semantic=client.semantic,
        limit=limit,
    )
    response = await _run_search(client, client.names.venues, body)
    hits = [_venue_hit(h, has_near=near is not None, has_q=bool(q)) for h in response["hits"]["hits"]]
    await _attach_event_names(ctx, hits)
    return {
        "query": q,
        "mode": "semantic" if (client.semantic and q) else "text",
        "total": _total(response),
        "hits": hits,
    }


async def _attach_event_names(ctx: AppContext, hits: list[dict[str, Any]]) -> None:
    """Fill `event_name` from the database for hits indexed before the event was (re)named."""
    missing = {h["event_id"] for h in hits if h.get("event_id") and not h.get("event_name")}
    if not missing:
        return
    async with ctx.db() as db:
        rows = (await db.execute(select(Event.id, Event.name).where(Event.id.in_(missing)))).all()
    names = {row.id: row.name for row in rows}
    for hit in hits:
        if not hit.get("event_name"):
            hit["event_name"] = names.get(hit.get("event_id"))


async def search_event_content(
    ctx: AppContext,
    *,
    event_id: str,
    q: str,
    types: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """One ranked list across an event's transcripts, chat messages and slides."""
    client = require_search_client(ctx)
    wanted = [t for t in (types or CONTENT_TYPES) if t in CONTENT_TYPES] or list(CONTENT_TYPES)
    indices = []
    if "transcript" in wanted:
        indices.append(client.names.transcripts)
    if "message" in wanted:
        indices.append(client.names.messages)
    if "slide" in wanted:
        indices.append(client.names.slides)
    body = build_event_content_query(q=q, event_id=event_id, limit=limit)
    response = await _run_search(client, indices, body)
    hits = [h for h in (_content_hit(raw, client.names) for raw in response["hits"]["hits"]) if h]
    return {"query": q, "types": wanted, "total": _total(response), "hits": hits}


async def search_session_transcript(ctx: AppContext, *, session_id: str, q: str, limit: int = 20) -> dict[str, Any]:
    """Transcript search for attendees of one live session."""
    client = require_search_client(ctx)
    body = build_session_transcript_query(q=q, session_id=session_id, limit=limit)
    response = await _run_search(client, client.names.transcripts, body)
    hits = []
    for raw in response["hits"]["hits"]:
        src = raw.get("_source") or {}
        hits.append(
            {
                "segment_id": src.get("segment_id"),
                "seq": src.get("seq"),
                "text": src.get("text"),
                "slide_index": src.get("slide_index"),
                "speaker": src.get("speaker"),
                "start_ms": src.get("start_ms"),
                "end_ms": src.get("end_ms"),
                "created_at": src.get("created_at"),
                "score": raw.get("_score"),
                "highlight": ((raw.get("highlight") or {}).get("text") or [None])[0],
            }
        )
    return {"query": q, "session_id": session_id, "total": _total(response), "hits": hits}


async def search_status(ctx: AppContext) -> dict[str, Any]:
    """Health of the search subsystem; never raises (used by `GET /search/status`)."""
    from app.search.indexer import get_indexer

    client = get_search_client(ctx)
    if client is None:
        return {"configured": False, "connected": False, "semantic": False, "indices": {}, "indexer": None}
    counts = await client.doc_counts() if client.connected else {}
    indexer = get_indexer(ctx)
    return {
        "configured": True,
        "url": client.url,
        "connected": client.connected,
        "cluster": client.cluster,
        "semantic": client.semantic,
        "inference_id": client.inference_id,
        "index_prefix": client.names.prefix,
        "indices": {
            "venues": {"name": client.names.venues, "docs": counts.get("venues")},
            "transcripts": {"name": client.names.transcripts, "docs": counts.get("transcripts")},
            "messages": {"name": client.names.messages, "docs": counts.get("messages")},
            "slides": {"name": client.names.slides, "docs": counts.get("slides")},
        },
        "last_error": client.last_error,
        "indexer": indexer.status() if indexer else None,
    }


# --- orchestrator hook ----------------------------------------------------------


async def search_venue_memory(
    ctx: AppContext,
    *,
    query: str,
    near: tuple[float, float] | None = None,
    radius_km: float | None = None,
    min_capacity: int | None = None,
    limit: int = 5,
    event_id: str | None = None,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    """Venues evaluated in earlier events, as compact dicts for the chat orchestrator to hand to Claude.

    Returns an empty list (never raises) when search is not configured or unreachable, so the
    orchestrator can treat "no memory" and "memory unavailable" the same way.

    Scoping: pass `event_id` (the event the chat belongs to) and results are limited to venues on
    events with the same owner, excluding the current event's own cards. `owner_id` can be given
    directly instead. With neither, results span every planner's events, so always pass one.
    """
    client = get_search_client(ctx)
    if client is None or not client.connected:
        return []
    if owner_id is None and event_id:
        async with ctx.db() as db:
            event = await db.get(Event, event_id)
        owner_id = event.owner_id if event else None
    try:
        result = await search_venues(
            ctx,
            owner_id=owner_id or "",
            q=query or None,
            near=near,
            radius_km=radius_km,
            min_capacity=min_capacity,
            exclude_event_id=event_id,
            limit=max(1, min(limit, 20)),
        )
    except SearchUnavailable as exc:
        log.info("search: venue memory unavailable: %s", exc)
        return []
    compact: list[dict[str, Any]] = []
    for hit in result["hits"]:
        compact.append(
            {
                "name": hit["name"],
                "address": hit["address"],
                "place_id": hit["place_id"],
                "distance_km": hit["distance_km"],
                "capacity_estimate": hit["capacity_estimate"],
                "capacity_fit": hit["capacity_fit"],
                "parking": hit["parking_summary"],
                "parking_score": hit["parking_score"],
                "weather_risk": hit["weather_risk"],
                "traffic_score": hit["traffic_score"],
                "score_overall": hit["score_overall"],
                "pros": hit["pros"][:4],
                "cons": hit["cons"][:4],
                "summary": (hit["summary"] or "")[:400] or None,
                "evaluated_for": {
                    "event_id": hit["event_id"],
                    "event_name": hit["event_name"],
                    "card_id": hit["card_id"],
                    "headcount": hit["criteria"]["headcount"],
                    "event_type": hit["criteria"]["event_type"],
                    "event_date": hit["criteria"]["event_date"],
                },
            }
        )
    return compact
