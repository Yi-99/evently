"""Elasticsearch indexer: subscribes to the event bus and keeps search indices in sync.

How it works
------------
`start_indexer(ctx)` is called from the app lifespan when `ELASTICSEARCH_URL` is set. It returns
immediately; everything else happens in three background tasks owned by `SearchIndexer`:

* **connect loop** - tries `SearchClient.connect()` (cluster ping, semantic-capability detection,
  index creation) and retries with exponential backoff (1 s -> 30 s) until it succeeds. The API keeps
  working while this runs; search routes answer 503 until the cluster is reachable.
* **discovery loop** - the bus has no wildcard subscription, and topics are per event/session, so
  every few seconds we read the `events` and `live_sessions` tables and subscribe to each
  `event:{id}` / `session:{id}` topic we have not seen yet. New subscriptions replay the topic's
  retained history (`since_seq=0`), which covers envelopes published between a row being created and
  the next poll; indexing is idempotent so replays are harmless. Topics for deleted events and ended
  sessions are dropped again (after draining what is still queued).
* **flush loop** - envelope handlers only enqueue bulk actions, keyed by `(index, _id)` so a card that
  changes ten times in a second is written once. The queue is flushed every ~1 s or as soon as 100
  actions are pending; transport failures keep the actions and back off. While the cluster is down
  the queue is bounded (`MAX_PENDING`): beyond that the oldest actions are dropped and counted, and a
  `POST /events/{id}/search/reindex` restores consistency.

`reindex_event(ctx, event_id)` rebuilds every document for one event straight from the database:
it deletes the event's documents and bulk-indexes the current rows. It is what the admin route runs and
how a cluster that was offline for a while (or a brand new one) is backfilled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from elasticsearch.exceptions import ApiError, TransportError
from sqlalchemy import select

from app.models import Card, ChatMessage, Event, LiveSession, Presentation, Slide, TranscriptSegment
from app.realtime import events as ev
from app.realtime.events import Envelope, event_topic, session_topic
from app.schemas.common import CardOut, ChatMessageOut, dump
from app.schemas.live import TranscriptSegmentOut
from app.schemas.presentation import SlideOut
from app.search.client import SearchClient, SearchUnavailable, get_search_client
from app.search.documents import message_doc, slide_doc, transcript_doc, venue_doc

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.realtime.bus import Subscription

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 1.0
FLUSH_BATCH_SIZE = 100
DISCOVERY_INTERVAL_S = 5.0
MAX_PENDING = 10_000
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
INDEXER_SERVICE_KEY = "search.indexer"


@dataclass
class _EventMeta:
    owner_id: str
    name: str


@dataclass
class _SessionMeta:
    event_id: str
    presentation_id: str | None


@dataclass
class _TopicState:
    subscription: Subscription
    task: asyncio.Task[None]


@dataclass
class IndexerStats:
    flushed: int = 0
    failed_items: int = 0
    dropped: int = 0
    flushes: int = 0
    last_flush_at: float | None = None
    last_error: str | None = None
    connect_attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class SearchIndexer:
    def __init__(self, ctx: AppContext, client: SearchClient) -> None:
        self.ctx = ctx
        self.client = client
        self.stats = IndexerStats()
        self._pending: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._query_deletes: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self._topics: dict[str, _TopicState] = {}
        self._events: dict[str, _EventMeta] = {}
        self._sessions: dict[str, _SessionMeta] = {}
        self._presentation_titles: dict[str, str] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._backoff_until = 0.0
        self._backoff = BACKOFF_MIN_S
        self._stopping = False

    # --- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._connect_loop(), name="search-connect"),
            asyncio.create_task(self._discovery_loop(), name="search-discovery"),
            asyncio.create_task(self._flush_loop(), name="search-flush"),
        ]

    async def stop(self) -> None:
        """Stop subscribing, flush what is pending (if the cluster is up) and close the client."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for topic in list(self._topics):
            await self._drop_topic(topic)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.client.connected and (self._pending or self._query_deletes):
            try:
                await self._flush()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                log.warning("search: final flush failed, %d actions lost: %s", len(self._pending), exc)
        await self.client.close()

    @property
    def subscribed_topics(self) -> int:
        return len(self._topics)

    @property
    def pending(self) -> int:
        return len(self._pending) + len(self._query_deletes)

    def status(self) -> dict[str, Any]:
        return {
            "subscribed_topics": self.subscribed_topics,
            "pending": self.pending,
            "flushes": self.stats.flushes,
            "flushed_docs": self.stats.flushed,
            "failed_items": self.stats.failed_items,
            "dropped": self.stats.dropped,
            "connect_attempts": self.stats.connect_attempts,
            "last_error": self.stats.last_error or self.client.last_error,
        }

    # --- background loops --------------------------------------------------------

    async def _connect_loop(self) -> None:
        delay = BACKOFF_MIN_S
        while not self._stopping:
            self.stats.connect_attempts += 1
            try:
                await self.client.connect()
                return
            except (TransportError, ApiError) as exc:
                level = logging.WARNING if self.stats.connect_attempts in (1, 5) else logging.DEBUG
                log.log(
                    level,
                    "search: cannot reach Elasticsearch at %s (%s); retrying in %.0fs",
                    self.client.url,
                    exc.__class__.__name__,
                    delay,
                )
            except Exception:  # noqa: BLE001 - never let the indexer die
                log.exception("search: unexpected error while connecting; retrying in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    async def _discovery_loop(self) -> None:
        while not self._stopping:
            try:
                await self._discover()
            except Exception:  # noqa: BLE001
                log.exception("search: topic discovery failed; will retry")
            await asyncio.sleep(DISCOVERY_INTERVAL_S)

    async def _discover(self) -> None:
        async with self.ctx.db() as db:
            events = (await db.execute(select(Event.id, Event.owner_id, Event.name))).all()
            sessions = (
                await db.execute(
                    select(LiveSession.id, LiveSession.event_id, LiveSession.presentation_id, LiveSession.status)
                )
            ).all()

        seen_events = {row.id for row in events}
        for row in events:
            self._events[row.id] = _EventMeta(owner_id=row.owner_id, name=row.name)
            await self._ensure_topic(event_topic(row.id))
        for event_id in list(self._events):
            if event_id not in seen_events:
                # Row gone; make sure its documents go too (the `event.deleted` envelope, if we got it,
                # already did this - delete_by_query is idempotent).
                self._enqueue_query_delete(self.client.names.all, {"term": {"event_id": event_id}})
                self._events.pop(event_id, None)
                await self._drop_topic(event_topic(event_id))

        active_sessions: set[str] = set()
        for row in sessions:
            self._sessions[row.id] = _SessionMeta(event_id=row.event_id, presentation_id=row.presentation_id)
            if row.status != "ended":
                active_sessions.add(row.id)
                await self._ensure_topic(session_topic(row.id))
        for topic in [t for t in self._topics if t.startswith("session:")]:
            if topic.removeprefix("session:") not in active_sessions:
                await self._drop_topic(topic)

    async def _flush_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), FLUSH_INTERVAL_S)
            except TimeoutError:
                pass
            self._wake.clear()
            if not self.client.connected or time.monotonic() < self._backoff_until:
                self._bound_pending()
                continue
            if not self._pending and not self._query_deletes:
                continue
            try:
                await self._flush()
                self._backoff = BACKOFF_MIN_S
            except (TransportError, ApiError) as exc:
                self.stats.last_error = str(exc)[:300]
                self._backoff_until = time.monotonic() + self._backoff
                log.warning(
                    "search: bulk write failed (%s); %d actions kept, retrying in %.0fs",
                    exc.__class__.__name__,
                    self.pending,
                    self._backoff,
                )
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
                if isinstance(exc, TransportError):
                    self.client.mark_disconnected(exc)
                    self._restart_connect_loop()
            except Exception:  # noqa: BLE001
                log.exception("search: flush failed unexpectedly; actions kept")
                self._backoff_until = time.monotonic() + self._backoff

    def _restart_connect_loop(self) -> None:
        if self._stopping:
            return
        if any(t.get_name() == "search-connect" and not t.done() for t in self._tasks):
            return
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(asyncio.create_task(self._connect_loop(), name="search-connect"))

    # --- topic subscriptions -----------------------------------------------------

    async def _ensure_topic(self, topic: str) -> None:
        if topic in self._topics or self._stopping:
            return
        subscription = await self.ctx.bus.subscribe(topic, since_seq=0)
        task = asyncio.create_task(self._consume(subscription), name=f"search-sub:{topic}")
        self._topics[topic] = _TopicState(subscription=subscription, task=task)

    async def _drop_topic(self, topic: str) -> None:
        state = self._topics.pop(topic, None)
        if state is None:
            return
        state.task.cancel()
        try:
            await state.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - we are discarding the consumer anyway
            pass
        # Deliver whatever was still queued before letting go of the subscription.
        queue = state.subscription.queue
        while not queue.empty():
            try:
                self._handle(queue.get_nowait())
            except asyncio.QueueEmpty:  # pragma: no cover - race guard
                break
            except Exception:  # noqa: BLE001
                log.exception("search: failed to handle a queued envelope on %s", topic)
        await state.subscription.close()

    async def _consume(self, subscription: Subscription) -> None:
        while True:
            envelope = await subscription.queue.get()
            try:
                self._handle(envelope)
            except Exception:  # noqa: BLE001 - one bad envelope must not kill the topic
                log.exception("search: failed to handle %s on %s", envelope.type, envelope.topic)

    # --- envelope handling -------------------------------------------------------

    def _handle(self, envelope: Envelope) -> None:
        data = envelope.data
        names = self.client.names
        kind, _, topic_id = envelope.topic.partition(":")

        if kind == "event":
            event_id = topic_id
            if envelope.type in (ev.BOARD_CARD_CREATED, ev.BOARD_CARD_UPDATED):
                card = data.get("card") or {}
                if card.get("kind") != "venue" or not card.get("id"):
                    return
                meta = self._events.get(event_id)
                doc = venue_doc(card, owner_id=meta.owner_id if meta else None, event_name=meta.name if meta else None)
                if doc is not None:
                    self._enqueue_index(names.venues, card["id"], doc)
            elif envelope.type == ev.BOARD_CARD_DELETED:
                if data.get("card_id"):
                    self._enqueue_delete(names.venues, data["card_id"])
            elif envelope.type in (ev.CHAT_MESSAGE_CREATED, ev.CHAT_MESSAGE_COMPLETED):
                message = data.get("message") or {}
                if envelope.type == ev.CHAT_MESSAGE_CREATED and message.get("role") != "user":
                    return  # assistant text is indexed once it is complete
                doc = message_doc(message)
                if doc is not None:
                    self._enqueue_index(names.messages, message["id"], doc)
            elif envelope.type in (ev.SLIDE_CREATED, ev.SLIDE_UPDATED):
                slide = data.get("slide") or {}
                if not slide.get("id"):
                    return
                title = self._presentation_titles.get(slide.get("presentation_id") or "")
                doc = slide_doc(slide, event_id=event_id, presentation_title=title)
                if doc is not None:
                    self._enqueue_index(names.slides, slide["id"], doc)
            elif envelope.type == ev.SLIDE_DELETED:
                if data.get("slide_id"):
                    self._enqueue_delete(names.slides, data["slide_id"])
            elif envelope.type in (ev.PRESENTATION_CREATED, ev.PRESENTATION_UPDATED):
                presentation = data.get("presentation") or {}
                if presentation.get("id") and presentation.get("title"):
                    self._presentation_titles[presentation["id"]] = presentation["title"]
            elif envelope.type == ev.PRESENTATION_DELETED:
                if data.get("presentation_id"):
                    self._presentation_titles.pop(data["presentation_id"], None)
                    self._enqueue_query_delete((names.slides,), {"term": {"presentation_id": data["presentation_id"]}})
            elif envelope.type == ev.EVENT_UPDATED:
                event = data.get("event") or {}
                meta = self._events.get(event_id)
                if meta and event.get("name"):
                    meta.name = event["name"]
            elif envelope.type == ev.EVENT_DELETED:
                self._enqueue_query_delete(names.all, {"term": {"event_id": event_id}})
                self._events.pop(event_id, None)
        elif kind == "session":
            session_id = topic_id
            if envelope.type == ev.SESSION_TRANSCRIPT_FINAL:
                segment = data.get("segment") or {}
                if not segment.get("id"):
                    return
                meta = self._sessions.get(session_id)
                doc = transcript_doc(
                    segment,
                    event_id=meta.event_id if meta else None,
                    presentation_id=meta.presentation_id if meta else None,
                )
                if doc is not None:
                    self._enqueue_index(names.transcripts, segment["id"], doc)
            elif envelope.type == ev.SESSION_UPDATED:
                session = data.get("session") or {}
                if session.get("id") and session.get("event_id"):
                    self._sessions[session["id"]] = _SessionMeta(
                        event_id=session["event_id"], presentation_id=session.get("presentation_id")
                    )

    # --- queue -------------------------------------------------------------------

    def _enqueue_index(self, index: str, doc_id: str, doc: dict[str, Any]) -> None:
        self._put((index, doc_id), {"_op_type": "index", "_index": index, "_id": doc_id, "_source": doc})

    def _enqueue_delete(self, index: str, doc_id: str) -> None:
        self._put((index, doc_id), {"_op_type": "delete", "_index": index, "_id": doc_id})

    def _put(self, key: tuple[str, str], action: dict[str, Any]) -> None:
        self._pending.pop(key, None)  # re-insert at the end so the newest write wins and order is kept
        self._pending[key] = action
        self._bound_pending()
        if len(self._pending) >= FLUSH_BATCH_SIZE:
            self._wake.set()

    def _enqueue_query_delete(self, indices: tuple[str, ...], query: dict[str, Any]) -> None:
        if (indices, query) not in self._query_deletes:
            self._query_deletes.append((indices, query))
        self._wake.set()

    def _bound_pending(self) -> None:
        dropped = 0
        while len(self._pending) > MAX_PENDING:
            self._pending.popitem(last=False)
            dropped += 1
        if dropped:
            self.stats.dropped += dropped
            log.warning(
                "search: %d pending actions dropped while Elasticsearch is unavailable; run a reindex later", dropped
            )

    async def _flush(self) -> None:
        actions = list(self._pending.values())
        self._pending.clear()
        try:
            ok, errors = await self.client.bulk(actions)
        except BaseException:
            # Put everything back, but never overwrite a newer action for the same document.
            for action in actions:
                key = (action["_index"], action["_id"])
                if key not in self._pending:
                    self._pending[key] = action
            raise
        self.stats.flushes += 1
        self.stats.flushed += ok
        self.stats.last_flush_at = time.time()
        for error in errors:
            self._record_item_error(error)

        while self._query_deletes:
            indices, query = self._query_deletes[0]
            await self.client.delete_by_query(indices, query)
            self._query_deletes.pop(0)

    def _record_item_error(self, error: Any) -> None:
        """One rejected bulk item: `{"index": {"_index": ..., "_id": ..., "status": 400, "error": {...}}}`.

        Rejections that reach here are not transient (429s were already retried by the bulk helper), so
        retrying the same document would fail again; it is logged, counted and dropped. A reindex of the
        event re-creates it once the cause (usually a mapping conflict) is fixed.
        """
        try:
            op, body = next(iter(error.items()))
        except (AttributeError, StopIteration):
            log.warning("search: unrecognised bulk error %r", error)
            return
        key = (body.get("_index"), body.get("_id"))
        self.stats.failed_items += 1
        reason = (body.get("error") or {}).get("reason") if isinstance(body.get("error"), dict) else body.get("error")
        self.stats.last_error = f"{op} {key[0]}/{key[1]}: {reason}"
        log.warning("search: bulk %s rejected for %s/%s (%s): %s", op, key[0], key[1], body.get("status"), reason)


# --- public entrypoints ----------------------------------------------------------


def get_indexer(ctx: AppContext) -> SearchIndexer | None:
    return ctx._services.get(INDEXER_SERVICE_KEY)


async def start_indexer(ctx: AppContext) -> None:
    """Create indices if missing and start the background subscriber; registers its own shutdown hook.

    Returns immediately. Index creation happens in the background so an unreachable cluster never
    delays or fails application startup; progress is logged and visible in `GET /search/status`.
    """
    client = get_search_client(ctx)
    if client is None:
        log.info("search: ELASTICSEARCH_URL not set; indexer disabled")
        return
    if get_indexer(ctx) is not None:
        return
    indexer = ctx.service(INDEXER_SERVICE_KEY, lambda: SearchIndexer(ctx, client))
    indexer.start()
    ctx.on_shutdown(indexer.stop)
    log.info("search: indexer started (prefix %s, target %s)", client.names.prefix, client.url)


async def reindex_event(ctx: AppContext, event_id: str) -> dict[str, int]:
    """Rebuild every search document for one event from the database.

    Deletes the event's existing documents, then bulk-indexes its venue cards, chat messages, slides
    and transcript segments. Returns the number of documents written per index. Raises
    `SearchUnavailable` when Elasticsearch is not configured or not reachable.
    """
    client = get_search_client(ctx)
    if client is None:
        raise SearchUnavailable("Search is not configured: set ELASTICSEARCH_URL.")
    client.require_connected()
    names = client.names

    async with ctx.db() as db:
        event = await db.get(Event, event_id)
        if event is None:
            raise LookupError(f"Event {event_id} not found")
        cards = (await db.execute(select(Card).where(Card.event_id == event_id, Card.kind == "venue"))).scalars()
        messages = (await db.execute(select(ChatMessage).where(ChatMessage.event_id == event_id))).scalars()
        presentations = (await db.execute(select(Presentation).where(Presentation.event_id == event_id))).scalars()
        presentations = list(presentations)
        presentation_ids = [p.id for p in presentations]
        slides = (
            list((await db.execute(select(Slide).where(Slide.presentation_id.in_(presentation_ids)))).scalars())
            if presentation_ids
            else []
        )
        sessions = list((await db.execute(select(LiveSession).where(LiveSession.event_id == event_id))).scalars())
        session_ids = [s.id for s in sessions]
        segments = (
            list(
                (
                    await db.execute(select(TranscriptSegment).where(TranscriptSegment.session_id.in_(session_ids)))
                ).scalars()
            )
            if session_ids
            else []
        )
        cards = list(cards)
        messages = list(messages)

    titles = {p.id: p.title for p in presentations}
    session_meta = {s.id: (s.event_id, s.presentation_id) for s in sessions}
    actions: list[dict[str, Any]] = []
    counts = {"venues": 0, "transcripts": 0, "messages": 0, "slides": 0}

    def add(index: str, label: str, doc_id: str, doc: dict[str, Any] | None) -> None:
        if doc is None:
            return
        actions.append({"_op_type": "index", "_index": index, "_id": doc_id, "_source": doc})
        counts[label] += 1

    for card in cards:
        add(
            names.venues,
            "venues",
            card.id,
            venue_doc(dump(CardOut, card), owner_id=event.owner_id, event_name=event.name),
        )
    for message in messages:
        add(names.messages, "messages", message.id, message_doc(dump(ChatMessageOut, message)))
    for slide in slides:
        add(
            names.slides,
            "slides",
            slide.id,
            slide_doc(dump(SlideOut, slide), event_id=event_id, presentation_title=titles.get(slide.presentation_id)),
        )
    for segment in segments:
        _, presentation_id = session_meta.get(segment.session_id, (event_id, None))
        add(
            names.transcripts,
            "transcripts",
            segment.id,
            transcript_doc(dump(TranscriptSegmentOut, segment), event_id=event_id, presentation_id=presentation_id),
        )

    await client.delete_by_query(names.all, {"term": {"event_id": event_id}}, refresh=True)
    ok, errors = await client.bulk(actions, refresh="wait_for")
    if errors:
        log.warning("search: reindex of %s wrote %d docs with %d errors: %s", event_id, ok, len(errors), errors[:3])
    log.info("search: reindexed event %s: %s", event_id, counts)
    return counts


async def reindex_owner(ctx: AppContext, owner_id: str) -> dict[str, dict[str, int]]:
    """Run `reindex_event` for every event the user owns. Returns counts per event id."""
    async with ctx.db() as db:
        event_ids = list((await db.execute(select(Event.id).where(Event.owner_id == owner_id))).scalars())
    results: dict[str, dict[str, int]] = {}
    for event_id in event_ids:
        results[event_id] = await reindex_event(ctx, event_id)
    return results
