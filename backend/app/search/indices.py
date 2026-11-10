"""Elasticsearch index names and mappings.

Four indices, all prefixed with `settings.elasticsearch_index_prefix` (default `evently`):

- `{prefix}-venues`       one document per venue card, across every event (the venue knowledge base)
- `{prefix}-transcripts`  one document per finalised transcript segment
- `{prefix}-messages`     one document per chat message
- `{prefix}-slides`       one document per slide (title, notes, keywords, outline text; never HTML)

Every document uses the database primary key as `_id` (card id, segment id, message id, slide id), so
indexing is an idempotent upsert and re-indexing is always safe.

The venues index has an optional `semantic` field of type `semantic_text`. It is only added when the
cluster offers an inference endpoint (detected at startup, see `app.search.client`); otherwise the same
content is indexed as plain `text` and search falls back to BM25 only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Analyzer shared by every free-text field: standard tokeniser + lowercase + English stemming, so
# "terraces" matches "terrace" and "parking lots" matches "parking lot".
_ANALYSIS = {
    "analysis": {
        "analyzer": {
            "evently_text": {
                "type": "custom",
                "tokenizer": "standard",
                "filter": ["lowercase", "asciifolding", "evently_stem"],
            }
        },
        "filter": {"evently_stem": {"type": "stemmer", "language": "light_english"}},
    }
}

INDEX_SETTINGS: dict[str, Any] = {"number_of_shards": 1, "number_of_replicas": 0, **_ANALYSIS}


def index_settings(*, serverless: bool) -> dict[str, Any]:
    """Index settings for this cluster flavour.

    Elastic Cloud Serverless manages sharding itself and rejects `number_of_shards` /
    `number_of_replicas` with a 400, so only the analysis settings are sent there.
    """
    return dict(_ANALYSIS) if serverless else dict(INDEX_SETTINGS)


def _text(**extra: Any) -> dict[str, Any]:
    return {"type": "text", "analyzer": "evently_text", **extra}


def _text_with_keyword(**extra: Any) -> dict[str, Any]:
    return _text(fields={"keyword": {"type": "keyword", "ignore_above": 256}}, **extra)


@dataclass(frozen=True)
class IndexNames:
    prefix: str

    @property
    def venues(self) -> str:
        return f"{self.prefix}-venues"

    @property
    def transcripts(self) -> str:
        return f"{self.prefix}-transcripts"

    @property
    def messages(self) -> str:
        return f"{self.prefix}-messages"

    @property
    def slides(self) -> str:
        return f"{self.prefix}-slides"

    @property
    def all(self) -> tuple[str, str, str, str]:
        return (self.venues, self.transcripts, self.messages, self.slides)

    @property
    def event_content(self) -> tuple[str, str, str]:
        """Indices searched by the per-event search (everything except the venue knowledge base)."""
        return (self.transcripts, self.messages, self.slides)


# Fields of the venue document that take part in full-text search, with boosts.
VENUE_TEXT_FIELDS = [
    "name^3",
    "title^2",
    "summary^2",
    "pros^1.5",
    "cons",
    "address",
    "types_text",
    "parking_summary",
    "weather_summary",
    "traffic_summary",
    "user_notes",
    "criteria_location",
    "criteria_event_type",
]

# Name of the semantic field (semantic_text when supported, plain text otherwise).
VENUE_SEMANTIC_FIELD = "semantic"


def venue_mapping(*, inference_id: str | None) -> dict[str, Any]:
    """Mapping for `{prefix}-venues`. `inference_id` enables `semantic_text`; None keeps it plain text."""
    semantic: dict[str, Any]
    if inference_id:
        semantic = {"type": "semantic_text", "inference_id": inference_id}
    else:
        semantic = _text()
    return {
        "dynamic": "false",
        "properties": {
            # identity / ownership
            "card_id": {"type": "keyword"},
            "event_id": {"type": "keyword"},
            "board_id": {"type": "keyword"},
            "owner_id": {"type": "keyword"},
            "event_name": _text_with_keyword(),
            "title": _text_with_keyword(),
            "status": {"type": "keyword"},
            "rank": {"type": "integer"},
            # place
            "name": _text_with_keyword(),
            "address": _text(),
            "place_id": {"type": "keyword"},
            "website": {"type": "keyword", "index": False},
            "maps_url": {"type": "keyword", "index": False},
            "phone": {"type": "keyword", "index": False},
            "location": {"type": "geo_point"},
            "types": {"type": "keyword"},
            "types_text": _text(),
            "rating": {"type": "float"},
            "review_count": {"type": "integer"},
            "price_level": {"type": "keyword"},
            # assessments
            "capacity_estimate": {"type": "integer"},
            "capacity_fit": {"type": "keyword"},
            "capacity_notes": _text(),
            "parking_on_site": {"type": "boolean"},
            "parking_summary": _text(),
            "parking_score": {"type": "integer"},
            "weather_summary": _text(),
            "weather_risk": {"type": "keyword"},
            "traffic_summary": _text(),
            "traffic_score": {"type": "integer"},
            "pricing_summary": _text(),
            "pricing_estimate": _text(),
            "within_budget": {"type": "keyword"},
            "score_overall": {"type": "integer"},
            "score_capacity": {"type": "integer"},
            "score_parking": {"type": "integer"},
            "score_weather": {"type": "integer"},
            "score_traffic": {"type": "integer"},
            "score_value": {"type": "integer"},
            "pros": _text(),
            "cons": _text(),
            "summary": _text(),
            "user_notes": _text(),
            # what the planner was looking for when this venue was researched
            "criteria_headcount": {"type": "integer"},
            "criteria_event_type": _text_with_keyword(),
            "criteria_event_date": {"type": "date"},
            "criteria_location": _text(),
            "criteria_setting": {"type": "keyword"},
            # natural-language blob (name, types, summary, pros, cons, notes) for semantic search
            VENUE_SEMANTIC_FIELD: semantic,
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    }


def transcript_mapping() -> dict[str, Any]:
    return {
        "dynamic": "false",
        "properties": {
            "segment_id": {"type": "keyword"},
            "session_id": {"type": "keyword"},
            "event_id": {"type": "keyword"},
            "presentation_id": {"type": "keyword"},
            "seq": {"type": "integer"},
            "text": _text(),
            "slide_index": {"type": "integer"},
            "speaker": {"type": "keyword"},
            "source": {"type": "keyword"},
            "confidence": {"type": "float"},
            "start_ms": {"type": "long"},
            "end_ms": {"type": "long"},
            "created_at": {"type": "date"},
        },
    }


def message_mapping() -> dict[str, Any]:
    return {
        "dynamic": "false",
        "properties": {
            "message_id": {"type": "keyword"},
            "event_id": {"type": "keyword"},
            "role": {"type": "keyword"},
            "content": _text(),
            "status": {"type": "keyword"},
            "created_at": {"type": "date"},
        },
    }


def slide_mapping() -> dict[str, Any]:
    return {
        "dynamic": "false",
        "properties": {
            "slide_id": {"type": "keyword"},
            "presentation_id": {"type": "keyword"},
            "event_id": {"type": "keyword"},
            "presentation_title": _text_with_keyword(),
            "position": {"type": "integer"},
            "title": _text_with_keyword(),
            "notes": _text(),
            "keywords": _text_with_keyword(),
            "outline_text": _text(),
            "status": {"type": "keyword"},
            "updated_at": {"type": "date"},
        },
    }


def all_mappings(names: IndexNames, *, inference_id: str | None) -> dict[str, dict[str, Any]]:
    """index name -> mapping, for every index this package manages."""
    return {
        names.venues: venue_mapping(inference_id=inference_id),
        names.transcripts: transcript_mapping(),
        names.messages: message_mapping(),
        names.slides: slide_mapping(),
    }
