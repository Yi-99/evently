"""Turn API payloads into Elasticsearch documents.

Every builder takes the JSON shape published on the event bus (`CardOut`, `TranscriptSegmentOut`,
`ChatMessageOut`, `SlideOut` dumps), so the live indexer and the database backfill share one code path:
the backfill serialises ORM rows with `app.schemas.common.dump` and calls the same functions.

Builders return None when the payload should not be indexed (non-venue cards, empty messages…).
"""

from __future__ import annotations

from typing import Any

from app.search.indices import VENUE_SEMANTIC_FIELD

_SUMMARY_MAX_CHARS = 4000


def _get(d: Any, *path: str) -> Any:
    """Nested lookup that tolerates missing/None sections (`_get(data, "parking", "score")`)."""
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None and str(v).strip()]


def _clip(text: str | None, limit: int = _SUMMARY_MAX_CHARS) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    return text[:limit] if text else None


def _drop_none(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if v is not None and v != [] and v != ""}


# --- venues ---------------------------------------------------------------------


def venue_semantic_text(card: dict[str, Any]) -> str:
    """Natural-language description of the venue for the semantic field.

    Combines what the semantic search user will describe: the kind of place, what it is good and bad
    at, how big it is and where it is. Kept well under typical inference chunk limits.
    """
    data = card.get("data") or {}
    place = data.get("place") or {}
    parts: list[str] = []
    name = place.get("name") or card.get("title")
    if name:
        parts.append(str(name))
    types = _str_list(place.get("types"))
    if types:
        parts.append("Type: " + ", ".join(t.replace("_", " ") for t in types) + ".")
    if place.get("address"):
        parts.append(f"Address: {place['address']}.")
    capacity = _int(_get(data, "capacity", "estimated_capacity"))
    if capacity:
        parts.append(f"Estimated capacity {capacity} people.")
    if _get(data, "capacity", "notes"):
        parts.append(str(data["capacity"]["notes"]))
    if data.get("summary"):
        parts.append(str(data["summary"]))
    pros = _str_list(data.get("pros"))
    if pros:
        parts.append("Pros: " + "; ".join(pros) + ".")
    cons = _str_list(data.get("cons"))
    if cons:
        parts.append("Cons: " + "; ".join(cons) + ".")
    for section in ("parking", "weather", "traffic", "pricing"):
        summary = _get(data, section, "summary")
        if summary:
            parts.append(f"{section.capitalize()}: {summary}")
    if data.get("user_notes"):
        parts.append(f"Planner notes: {data['user_notes']}")
    return _clip(" ".join(parts), 6000) or ""


def venue_doc(card: dict[str, Any], *, owner_id: str | None, event_name: str | None = None) -> dict[str, Any] | None:
    """Document for `{prefix}-venues` from a `CardOut` dict, or None if the card is not a venue card."""
    if card.get("kind") != "venue":
        return None
    data = card.get("data") or {}
    if not isinstance(data, dict):
        return None
    place = data.get("place") or {}
    if not isinstance(place, dict):
        place = {}
    criteria = data.get("criteria") or {}
    if not isinstance(criteria, dict):
        criteria = {}
    lat, lng = _float(place.get("lat")), _float(place.get("lng"))
    location = {"lat": lat, "lon": lng} if lat is not None and lng is not None else None
    types = _str_list(place.get("types"))

    doc = {
        "card_id": card["id"],
        "event_id": card.get("event_id"),
        "board_id": card.get("board_id"),
        "owner_id": owner_id,
        "event_name": event_name,
        "title": card.get("title") or place.get("name"),
        "status": card.get("status"),
        "rank": _int(data.get("rank")),
        "name": place.get("name") or card.get("title"),
        "address": place.get("address"),
        "place_id": place.get("place_id"),
        "website": place.get("website"),
        "maps_url": place.get("maps_url"),
        "phone": place.get("phone"),
        "location": location,
        "types": types,
        "types_text": " ".join(t.replace("_", " ") for t in types) or None,
        "rating": _float(place.get("rating")),
        "review_count": _int(place.get("review_count")),
        "price_level": place.get("price_level"),
        "capacity_estimate": _int(_get(data, "capacity", "estimated_capacity")),
        "capacity_fit": _get(data, "capacity", "fits_headcount"),
        "capacity_notes": _clip(_get(data, "capacity", "notes")),
        "parking_on_site": _get(data, "parking", "on_site"),
        "parking_summary": _clip(_get(data, "parking", "summary")),
        "parking_score": _int(_get(data, "parking", "score")),
        "weather_summary": _clip(_get(data, "weather", "summary")),
        "weather_risk": _get(data, "weather", "risk"),
        "traffic_summary": _clip(_get(data, "traffic", "summary")),
        "traffic_score": _int(_get(data, "traffic", "score")),
        "pricing_summary": _clip(_get(data, "pricing", "summary")),
        "pricing_estimate": _clip(_get(data, "pricing", "estimate")),
        "within_budget": _get(data, "pricing", "within_budget"),
        "score_overall": _int(_get(data, "score", "overall")),
        "score_capacity": _int(_get(data, "score", "capacity")),
        "score_parking": _int(_get(data, "score", "parking")),
        "score_weather": _int(_get(data, "score", "weather")),
        "score_traffic": _int(_get(data, "score", "traffic")),
        "score_value": _int(_get(data, "score", "value")),
        "pros": _str_list(data.get("pros")),
        "cons": _str_list(data.get("cons")),
        "summary": _clip(data.get("summary")),
        "user_notes": _clip(data.get("user_notes")),
        "criteria_headcount": _int(criteria.get("headcount")),
        "criteria_event_type": criteria.get("event_type"),
        "criteria_event_date": criteria.get("event_date"),
        "criteria_location": criteria.get("location"),
        "criteria_setting": criteria.get("setting"),
        VENUE_SEMANTIC_FIELD: venue_semantic_text(card) or None,
        "created_at": card.get("created_at"),
        "updated_at": card.get("updated_at"),
    }
    return _drop_none(doc)


# --- transcripts ----------------------------------------------------------------


def transcript_doc(
    segment: dict[str, Any], *, event_id: str | None, presentation_id: str | None = None
) -> dict[str, Any] | None:
    """Document for `{prefix}-transcripts` from a `TranscriptSegmentOut` dict."""
    text = (segment.get("text") or "").strip()
    if not text:
        return None
    return _drop_none(
        {
            "segment_id": segment["id"],
            "session_id": segment.get("session_id"),
            "event_id": event_id,
            "presentation_id": presentation_id,
            "seq": _int(segment.get("seq")),
            "text": text,
            "slide_index": _int(segment.get("slide_index")),
            "speaker": segment.get("speaker"),
            "source": segment.get("source"),
            "confidence": _float(segment.get("confidence")),
            "start_ms": _int(segment.get("start_ms")),
            "end_ms": _int(segment.get("end_ms")),
            "created_at": segment.get("created_at"),
        }
    )


# --- chat messages --------------------------------------------------------------


def message_doc(message: dict[str, Any]) -> dict[str, Any] | None:
    """Document for `{prefix}-messages` from a `ChatMessageOut` dict; None for empty or system messages."""
    content = (message.get("content") or "").strip()
    role = message.get("role")
    if not content or role not in ("user", "assistant"):
        return None
    return _drop_none(
        {
            "message_id": message["id"],
            "event_id": message.get("event_id"),
            "role": role,
            "content": content[:20000],
            "status": message.get("status"),
            "created_at": message.get("created_at"),
        }
    )


# --- slides ---------------------------------------------------------------------


def slide_outline_text(outline: Any) -> str | None:
    """Readable text from a `SlideOutline` dict (purpose, key points, visual idea). Never the HTML."""
    if not isinstance(outline, dict):
        return None
    parts: list[str] = []
    for key in ("purpose", "visual"):
        if outline.get(key):
            parts.append(str(outline[key]))
    parts.extend(_str_list(outline.get("key_points")))
    return _clip(" ".join(parts))


def slide_doc(
    slide: dict[str, Any], *, event_id: str | None, presentation_title: str | None = None
) -> dict[str, Any] | None:
    """Document for `{prefix}-slides` from a `SlideOut` dict (title, notes, keywords, outline; no HTML)."""
    return _drop_none(
        {
            "slide_id": slide["id"],
            "presentation_id": slide.get("presentation_id"),
            "event_id": event_id,
            "presentation_title": presentation_title,
            "position": _int(slide.get("position")),
            "title": slide.get("title"),
            "notes": _clip(slide.get("notes"), 20000),
            "keywords": _str_list(slide.get("keywords")),
            "outline_text": slide_outline_text(slide.get("outline")),
            "status": slide.get("status"),
            "updated_at": slide.get("updated_at"),
        }
    )
