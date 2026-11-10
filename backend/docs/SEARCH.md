# Search: venue memory and full-text search (Elasticsearch)

Evently keeps an Elasticsearch copy of everything worth searching: every venue card ever researched
(across all of a planner's events), finalised live-transcript segments, chat messages and slide text.
It powers three things:

1. **Venue memory** — "have we evaluated a quiet venue with a terrace for 150 near the docks before?"
   Hybrid full-text/semantic search with geo, capacity, parking and score filters, across events.
   Also exposed to the chat orchestrator as a tool (`search_venue_memory`).
2. **Event search** — one ranked list over an event's transcripts, chat and slides with deep links.
3. **Live transcript search** — attendees search what the speaker has said so far during a talk.

Search is optional. Without `ELASTICSEARCH_URL` nothing runs and the search routes answer
`503 Search is not configured`. Everything else in the API works exactly the same.

Code: `app/search/` (`client.py`, `indices.py`, `documents.py`, `indexer.py`, `service.py`) and the
routes in `app/api/search.py`.

## How it fits together

```
 board / chat / deck / live services ──publish──▶ EventBus (event:{id}, session:{id} topics)
                                                        │
                                   SearchIndexer ◀──subscribe (one subscription per topic)
                                   • discovers topics by polling events + live_sessions tables
                                   • turns envelopes into documents (app/search/documents.py)
                                   • batches writes: bulk every ~1 s or 100 docs, retries with backoff
                                                        │
                                                        ▼
                                  Elasticsearch: {prefix}-venues / -transcripts / -messages / -slides
                                                        ▲
                       GET /search/venues, /events/{id}/search, /live-sessions/{id}/search
                       app/search/service.py (query builders + hit shaping)
```

No other package knows about search: the indexer only consumes the envelopes that already drive the
frontend (`app/realtime/events.py`). The database stays the source of truth; the indices can be
dropped and rebuilt at any time with the reindex route.

## Configuration

| Variable | Meaning |
|---|---|
| `ELASTICSEARCH_URL` | Cluster URL, e.g. `http://localhost:9200`, `http://elasticsearch:9200` (docker compose) or an Elastic Cloud endpoint `https://<deployment>.es.<region>.gcp.elastic-cloud.com`. Unset = search disabled. |
| `ELASTICSEARCH_API_KEY` | API key (base64 `id:key` form as shown by Kibana / the Cloud console). Needed for Elastic Cloud; leave empty for an unsecured local container. |
| `ELASTICSEARCH_INDEX_PREFIX` | Index name prefix, default `evently`. Use a different prefix per environment sharing a cluster (`evently-dev`, `evently-prod`). |
| `ELASTICSEARCH_INFERENCE_ID` | *(environment variable only, optional)* Inference endpoint for semantic search. `none` forces plain-text mode; an endpoint id (e.g. `.elser-2-elasticsearch` or `my-e5`) forces that endpoint. Unset = auto-detect (see [Semantic vs. plain mode](#semantic-vs-plain-mode)). |

### Running Elasticsearch locally (docker compose)

A single-node Elasticsearch 9 with security off is enough for development. Add this service to the
project's `docker-compose.yml` (service name `elasticsearch`, so the API container reaches it at
`http://elasticsearch:9200`; from your laptop use `http://localhost:9200`):

```yaml
services:
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:9.1.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - xpack.ml.enabled=true            # needed for semantic search (ELSER); set false to save RAM
      - ES_JAVA_OPTS=-Xms2g -Xmx2g        # 4g+ if you want ELSER to allocate
    ports:
      - "9200:9200"
    volumes:
      - esdata:/usr/share/elasticsearch/data
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:9200/_cluster/health || exit 1"]
      interval: 10s
      retries: 12

volumes:
  esdata:
```

Then in `.env`:

```
ELASTICSEARCH_URL=http://localhost:9200
ELASTICSEARCH_API_KEY=
ELASTICSEARCH_INDEX_PREFIX=evently
```

Start the API as usual; you should see `search: indexer started` and, once the cluster answers,
`search: connected to Elasticsearch 9.x (...), semantic=..., inference_id=...` in the logs.
`GET /api/v1/search/status` shows the same information.

Without ML (`xpack.ml.enabled=false` or too little memory) the cluster still lists the preconfigured
`.elser-2-elasticsearch` endpoint but cannot allocate the model. The indexer notices (the endpoint
fails a probe request within 90 s) and falls back to plain-text mode; set
`ELASTICSEARCH_INFERENCE_ID=none` to skip the probe and the warning.

### Elastic Cloud (hosted or serverless)

1. Create a deployment / project and copy its Elasticsearch endpoint URL.
2. Kibana → Stack Management → API keys → create an API key for a user that may manage indices
   (`manage_index_templates` is not needed; the privileges `create_index`, `manage`, `read`, `write`
   on `evently-*` plus `monitor` on the cluster and `manage_inference` for the endpoint lookup are
   sufficient). Copy the **encoded** (base64) value.
3. Set `ELASTICSEARCH_URL=https://...` and `ELASTICSEARCH_API_KEY=<encoded key>`.

Hosted deployments with an ML node, and all serverless projects, offer `.elser-2-elasticsearch` out
of the box, so semantic mode is detected automatically.

## What is indexed

All documents use the database primary key as `_id`, so every write is an idempotent upsert and a
replay or reindex can never create duplicates.

| Index | One document per | `_id` | Written on |
|---|---|---|---|
| `{prefix}-venues` | venue card (`Card.kind == "venue"`), across all events | card id | `board.card.created`, `board.card.updated`; removed on `board.card.deleted`, `event.deleted` |
| `{prefix}-transcripts` | finalised transcript segment | segment id | `session.transcript.final`; removed on `event.deleted` |
| `{prefix}-messages` | chat message (user and assistant; system messages and empty bodies are skipped) | message id | `chat.message.created` (user), `chat.message.completed` (assistant); removed on `event.deleted` |
| `{prefix}-slides` | slide: title, speaker notes, keywords, outline text — **never the HTML** | slide id | `slide.created`, `slide.updated`; removed on `slide.deleted`, `presentation.deleted`, `event.deleted` |

Other envelopes are used only for bookkeeping: `presentation.created/updated` (deck title cached onto
slide docs), `event.updated` (event name on venue docs), `session.updated` (session → event mapping).

### Mappings (`app/search/indices.py`)

All text fields share one analyzer (`standard` tokenizer, lowercase, ASCII folding, light English
stemming), so "terraces" matches "terrace". Mappings are `dynamic: false`: unknown fields are stored
but not indexed, so a payload change can never break indexing.

**venues** (flattened `VenueCardData`, see `app/schemas/venue.py`):

| Field(s) | Type | From |
|---|---|---|
| `card_id`, `event_id`, `board_id`, `owner_id`, `status` | keyword | card / event owner |
| `event_name`, `title`, `name` | text + `.keyword` | event, card title, `place.name` |
| `address`, `types_text` | text | `place.address`, `place.types` as words |
| `types` | keyword | `place.types` |
| `location` | **geo_point** | `place.lat` / `place.lng` |
| `place_id`, `price_level` | keyword | `place` |
| `website`, `maps_url`, `phone` | keyword (not indexed) | `place` |
| `rating` (float), `review_count` (int) | | `place` |
| `capacity_estimate` (int), `capacity_fit` (keyword), `capacity_notes` (text) | | `capacity` |
| `parking_on_site` (bool), `parking_summary` (text), `parking_score` (int) | | `parking` |
| `weather_summary` (text), `weather_risk` (keyword) | | `weather` |
| `traffic_summary` (text), `traffic_score` (int) | | `traffic` |
| `pricing_summary`, `pricing_estimate` (text), `within_budget` (keyword) | | `pricing` |
| `score_overall`, `score_capacity`, `score_parking`, `score_weather`, `score_traffic`, `score_value` | int | `score` |
| `pros`, `cons`, `summary`, `user_notes` | text | card data |
| `criteria_headcount` (int), `criteria_event_type` (text + keyword), `criteria_event_date` (date), `criteria_location` (text), `criteria_setting` (keyword) | | `criteria` (what the planner was looking for) |
| `semantic` | **semantic_text** (or text in plain mode) | a generated paragraph: name, types, address, capacity, summary, pros, cons, parking/weather/traffic/pricing summaries, planner notes |
| `rank` (int), `created_at`, `updated_at` (date) | | card |

**transcripts**: `segment_id`, `session_id`, `event_id`, `presentation_id` (keyword), `seq`,
`slide_index` (int), `text` (text), `speaker`, `source` (keyword), `confidence` (float),
`start_ms`, `end_ms` (long), `created_at` (date).

**messages**: `message_id`, `event_id`, `role`, `status` (keyword), `content` (text), `created_at` (date).

**slides**: `slide_id`, `presentation_id`, `event_id`, `status` (keyword), `presentation_title`,
`title`, `keywords` (text + `.keyword`), `notes`, `outline_text` (text), `position` (int), `updated_at` (date).

## Semantic vs. plain mode

Semantic mode makes "quiet venue with a terrace for 150" find a card whose summary says "calm rooftop
space, room for about 200" even though no words overlap. It relies on Elasticsearch's
`semantic_text` field type, which embeds text with an **inference endpoint** at index time and at
query time.

At connect time the client decides the mode once per index:

1. `ELASTICSEARCH_INFERENCE_ID=none` → plain. A specific id → that endpoint, no probe.
2. Otherwise `GET _inference/_all`; prefer `.elser-2-elasticsearch`, else the first `sparse_embedding`
   endpoint, else the first `text_embedding` endpoint.
3. The chosen endpoint must answer a probe (`POST _inference/<id>` with a short sentence) within 90 s.
   This is what allocates the model on first use; on a cluster without ML capacity it fails and we fall
   back to plain mode with a warning.
4. If `{prefix}-venues` **already exists**, its mapping wins: the mode is read from whether the
   `semantic` field is `semantic_text`. Mappings are never migrated in place.

`GET /search/status` reports `semantic: true|false` and the `inference_id` in use; venue search
responses carry `"mode": "semantic" | "text"`.

How search differs:

| | plain | semantic |
|---|---|---|
| Venue query | BM25 `multi_match` over name, title, summary, pros, cons, address, types, parking/weather/traffic summaries, notes, criteria (fuzzy) | the same BM25 clause **plus** a `semantic` query on the `semantic` field, combined in one `bool.should` (scores add up) |
| Filters | identical: owner, geo-distance, capacity, parking, score, event type | |
| Highlights | text fields | text fields (semantic matches highlight nothing by themselves) |
| Event / session search | BM25 only in both modes (transcripts, chat and slides are keyword content) | |

Switching modes later: delete `{prefix}-venues` (`DELETE evently-venues` in Kibana Dev Tools or
`curl -XDELETE $ELASTICSEARCH_URL/evently-venues`), restart the API so the index is recreated with the
new mapping, then run the reindex (below). If the cluster is licensed for the `rrf` retriever, replacing
the `bool.should` combination in `build_venue_query` with reciprocal rank fusion is a one-function change.

## Routes

All routes live under `/api/v1`. Owner routes need the owner's access token (or no token in dev when
`AUTH_REQUIRED` is off). Every route returns **`503`** with `{"detail": "..."}` when Elasticsearch
is not configured or not reachable (the message says which).

### `GET /search/venues` — venue memory (owner)

Venues evaluated on any of the caller's events; cards from other users are never returned.

| Query param | Meaning |
|---|---|
| `q` | Free text, e.g. `quiet venue with a terrace for 150`. Optional: with filters only, results are sorted by overall score. |
| `near` | `lat,lng`. Filters to `radius_km` and sorts by distance (after relevance when `q` is given). |
| `radius_km` | Radius around `near`, default 25. |
| `min_capacity` | `capacity_estimate >= n`. |
| `parking` | `true` = on-site parking **or** parking score ≥ 60. |
| `min_score` | `score_overall >= n` (0–100). |
| `event_type` | Matches the event type the venue was researched for (`criteria.event_type`). |
| `exclude_event_id` | Leave out the current event's own cards. |
| `limit` | 1–100, default 20. |

```
GET /api/v1/search/venues?q=quiet%20venue%20with%20a%20terrace%20for%20150&near=53.349,-6.26&radius_km=10&min_capacity=150&parking=true&min_score=60&limit=5
```

```json
{
  "query": "quiet venue with a terrace for 150",
  "mode": "semantic",
  "total": 3,
  "hits": [
    {
      "card_id": "card_…", "event_id": "evt_…", "event_name": "Q4 offsite",
      "name": "The Foundry", "address": "12 Dock St, Dublin", "place_id": "ChIJ…",
      "lat": 53.34, "lng": -6.25, "distance_km": 1.42,
      "types": ["event_venue", "bar"], "rating": 4.6, "review_count": 212,
      "capacity_estimate": 200, "capacity_fit": "yes",
      "parking_on_site": false, "parking_summary": "Q-Park 300 m away", "parking_score": 55,
      "weather_summary": "Mild, 40% rain", "weather_risk": "medium",
      "traffic_summary": "Rush-hour overlap", "traffic_score": 40,
      "pricing_estimate": "EUR 4-6k", "score_overall": 78,
      "pros": ["Quiet terrace", "Great acoustics"], "cons": ["No on-site parking"],
      "summary": "A converted warehouse with a quiet rooftop terrace.",
      "criteria": {"headcount": 150, "event_type": "offsite", "event_date": "2026-12-04", "location": "Dublin"},
      "status": "ready", "updated_at": "2026-11-09T10:05:00Z",
      "relevance": 12.7,
      "highlights": {"summary": ["A converted warehouse with a <em>quiet</em> rooftop <em>terrace</em>."]}
    }
  ]
}
```

`card_id` + `event_id` are the deep link back to the board where the venue was evaluated.

### `GET /events/{event_id}/search` — everything in one event (owner)

| Query param | Meaning |
|---|---|
| `q` | Required. |
| `types` | Comma-separated subset of `transcript,message,slide` (default all). |
| `limit` | 1–100, default 20. |

```
GET /api/v1/events/evt_123/search?q=rush%20hour&types=transcript,message
```

```json
{
  "query": "rush hour", "types": ["transcript", "message"], "total": 4,
  "hits": [
    {"type": "transcript", "id": "seg_…", "event_id": "evt_123", "score": 6.1,
     "text": "…and remember the rush hour starts at four…",
     "link": {"session_id": "ses_…", "seq": 42, "segment_id": "seg_…", "slide_index": 3},
     "speaker": null, "start_ms": 1830000, "end_ms": 1834200, "created_at": "…",
     "highlights": {"text": ["remember the <em>rush</em> <em>hour</em> starts at four"]}},
    {"type": "message", "id": "msg_…", "event_id": "evt_123", "score": 4.9,
     "text": "Can you check rush hour traffic from the airport?",
     "link": {"message_id": "msg_…"}, "role": "user", "created_at": "…",
     "highlights": {"content": ["Can you check <em>rush</em> <em>hour</em> traffic…"]}},
    {"type": "slide", "id": "sld_…", "event_id": "evt_123", "score": 3.2,
     "title": "Getting there", "text": "Mention the shuttle; rush hour is 16:00–19:00.",
     "link": {"slide_id": "sld_…", "presentation_id": "prs_…", "position": 5},
     "presentation_title": "Offsite deck", "keywords": ["getting there"], "updated_at": "…",
     "highlights": {"notes": ["…<em>rush</em> <em>hour</em> is 16:00–19:00."]}}
  ]
}
```

`link` holds what a client needs to jump to the hit: `session_id` + `seq` for a transcript line,
`message_id` for chat, `slide_id` / `presentation_id` / `position` for a slide.

### `GET /live-sessions/{session_id}/search` — transcript so far (participants)

Authenticated like the other session routes (`app.live.service.authenticate_session_client`):
`?token=<participant token>` (from `POST /live-sessions/join`) or the owner's access token as
`?token=` or `Authorization: Bearer`. `401` without a valid token, `404` for an unknown session.

```
GET /api/v1/live-sessions/ses_123/search?q=pricing&token=eyJ…
```

```json
{
  "query": "pricing", "session_id": "ses_123", "total": 2,
  "hits": [
    {"segment_id": "seg_…", "seq": 17, "text": "Let's talk about pricing tiers.", "slide_index": 4,
     "speaker": null, "start_ms": 912000, "end_ms": 915300, "created_at": "…", "score": 5.4,
     "highlight": "Let's talk about <em>pricing</em> tiers."}
  ]
}
```

Segments appear in search about a second after they are finalised (bulk flush + index refresh).

### `POST /events/{event_id}/search/reindex` — rebuild one event (owner)

Deletes the event's documents in all four indices and re-indexes its venue cards, chat messages,
slides and transcript segments from the database. Synchronous; returns counts:

```json
{"event_id": "evt_123", "indexed": {"venues": 6, "transcripts": 412, "messages": 38, "slides": 12}}
```

### `POST /search/reindex` — rebuild every event the caller owns

Same, for all of the caller's events (backfill after enabling search for the first time):

```json
{"events": {"evt_1": {"venues": 6, "...": 0}, "evt_2": {"...": 0}}, "totals": {"venues": 9, "transcripts": 412, "messages": 80, "slides": 20}}
```

### `GET /search/status`

```json
{
  "configured": true, "url": "http://localhost:9200", "connected": true,
  "cluster": {"name": "docker-cluster", "version": "9.1.0"},
  "semantic": true, "inference_id": ".elser-2-elasticsearch", "index_prefix": "evently",
  "indices": {
    "venues": {"name": "evently-venues", "docs": 42},
    "transcripts": {"name": "evently-transcripts", "docs": 1830},
    "messages": {"name": "evently-messages", "docs": 211},
    "slides": {"name": "evently-slides", "docs": 64}
  },
  "last_error": null,
  "indexer": {"subscribed_topics": 7, "pending": 0, "flushes": 128, "flushed_docs": 2147,
              "failed_items": 0, "dropped": 0, "connect_attempts": 1, "last_error": null}
}
```

`configured: false` means `ELASTICSEARCH_URL` is unset. `connected: false` with `configured: true`
means the indexer is still retrying (see `last_error`); writes are queued meanwhile (`pending`).

## The indexer in detail (`app/search/indexer.py`)

`start_indexer(ctx)` is called from the app lifespan when `ELASTICSEARCH_URL` is set. It returns at
once and runs three background tasks:

- **connect loop** — `SearchClient.connect()`: ping, inference detection, `ensure_indices()` (creates
  missing indices; existing ones are left alone). Retries with backoff 1 s → 30 s until it succeeds, so
  a cluster that is still booting, or down, never blocks or fails API startup.
- **discovery loop** — every 5 s reads `events` and `live_sessions` and subscribes to each
  `event:{id}` topic and each non-ended `session:{id}` topic it does not have yet. The bus has no
  wildcard subscription, and topics are per event/session, so this is how the indexer sees everything.
  A new subscription replays the topic's retained history (`since_seq=0`), which covers envelopes
  published between the row's creation and the next poll. Topics of deleted events / ended sessions
  are dropped (after draining what is still queued); a vanished event also triggers a
  delete-by-query, in case the `event.deleted` envelope was missed.
- **flush loop** — handlers only enqueue bulk actions keyed by `(index, _id)`, so a card patched ten
  times in a second is written once (newest wins, a delete replaces an index). The queue is flushed
  every ~1 s or as soon as 100 actions are waiting. Transport failures keep the actions, mark the
  client disconnected, back off (1 s → 30 s) and restart the connect loop; the bulk helper itself retries
  `429` items. Non-transient per-document rejections (e.g. a mapping conflict) are logged, counted
  (`failed_items`) and dropped. While the cluster is down the queue is bounded at 10 000 actions:
  older ones are dropped and counted (`dropped`); a reindex restores consistency.

On shutdown (`ctx.on_shutdown`) the indexer stops subscribing, flushes what is pending if the cluster
is reachable, and closes the client.

Multiple API workers: each worker runs its own indexer and would write every document several times.
That is harmless (idempotent upserts) but wasteful; with `REDIS_URL` set, the Redis bus delivers every
envelope to every worker, so run the indexer on one worker only (start the others with
`ELASTICSEARCH_URL` unset and point their search routes at the same cluster via a shared `.env` if
needed), or accept the duplicate writes.

## Reindex procedure

Use it after enabling search on an existing database, after a cluster outage long enough to drop
pending writes (`dropped > 0` in `/search/status`), after changing a mapping, or whenever the index
looks out of sync with the board.

1. Make sure `GET /api/v1/search/status` shows `connected: true`.
2. For one event: `POST /api/v1/events/{event_id}/search/reindex`.
   For everything you own: `POST /api/v1/search/reindex`.
3. Both are synchronous and return the document counts; the indices are refreshed before the
   response, so a search immediately afterwards sees the new documents.

To change a mapping (including switching semantic mode): delete the affected index, restart the API
(indices are created at connect time with the current mapping) and run step 2. Deleting all four
`evently-*` indices and reindexing is always safe; the database is the source of truth.

From code, `app.search.indexer.reindex_event(ctx, event_id)` returns the same counts and raises
`SearchUnavailable` when the cluster is not reachable.

## Orchestrator hook

`app/search/service.py` exposes:

```python
async def search_venue_memory(
    ctx, *, query: str, near: tuple[float, float] | None = None, radius_km: float | None = None,
    min_capacity: int | None = None, limit: int = 5, event_id: str | None = None, owner_id: str | None = None,
) -> list[dict]
```

It returns compact dicts (name, address, place_id, distance_km, capacity, parking, weather risk,
traffic score, overall score, up to four pros/cons, a 400-character summary and `evaluated_for`:
event id/name, card id, headcount, event type and date). Pass `event_id` of the current chat: results
are then scoped to the same owner's events and the current event's own cards are excluded. It never
raises — when search is off or unreachable it returns `[]`, so the orchestrator can simply say it has no
memory of earlier venues.

The chat orchestrator (`app/agents/orchestrator`) uses search in three ways. All of them go through
`app.search.service` (imported as a module, so tests can monkeypatch it) and none of them requires
search to be configured.

### Tools

`search_venue_memory` — venues researched for this planner's earlier events, with scores and caveats.
The model is told to use it before launching a new venue search and when the planner asks "have we
looked at X before". Input:

```json
{"query": "string", "near": {"lat": 0.0, "lng": 0.0}, "radius_km": 5.0, "min_capacity": 100, "limit": 5}
```

Only `query` is required; `limit` is 1-10 (default 5); `radius_km` applies only with `near`. The
handler calls `search_venue_memory(..., event_id=<this event>)` and returns the compact list. When the
list is empty it returns `{"hits": [], "note": ...}` where the note says whether venue memory is not
available (Elasticsearch not configured, or unreachable) or that there were simply no matches.

`search_event_history` — what was said in past talks, earlier chat or slides of this event. Input:

```json
{"query": "string", "types": ["transcript", "message", "slide"], "limit": 8}
```

`types` is optional (all three by default); `limit` is 1-20. It calls `search_event_content` scoped to
this event and returns `{"total": n, "hits": [...]}`; each hit has `type`, a text `snippet` (300
characters at most) and `link` ids (`session_id`/`seq`/`segment_id`/`slide_index` for transcripts,
`message_id` for chat, `slide_id`/`presentation_id`/`position` for slides), plus title, speaker or role
where they exist. If search is off or unreachable the tool reports a `ToolError` ("Event history search
is not available: ...") for the model to relay.

### Automatic `<memory>` block

When `ELASTICSEARCH_URL` is set, each turn first runs one `search_venue_memory` query before the user
turn is built: the planner's message text (first 500 characters) plus `requirements.location` when
saved, limit 3, under a 1.5 second `asyncio.wait_for` timeout. Any exception or timeout skips the step
silently, and when search is not configured nothing is attempted, so a turn is never delayed past
1.5 s or broken by search. If there are hits, a `<memory>` block follows the `<workspace>` block in the
current user turn, one line per venue: name, address, overall score, the headcount it was evaluated
for and a one-line summary. The block lives only in the user turn; the system prompt stays frozen and
only carries a sentence saying that memory results are prior research and may be stale.
