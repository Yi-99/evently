"""Redis-backed EventBus for multi-worker deployments (optional; needs the `redis` extra).

Layout in Redis (all keys share `prefix`):

- ``{prefix}:seq:{topic}``  INCR counter → per-topic seq, so numbering survives restarts
- ``{prefix}:log:{topic}``  capped list of envelope JSON (newest last) for replay, with a TTL
- ``{prefix}:bus:{topic}``  pub/sub channel carrying the same JSON

Every worker PSUBSCRIBEs to ``{prefix}:bus:*`` once; a single reader task turns incoming
messages into `Envelope`s, appends them to a local ring buffer and pushes them to the
local `Subscription`s. The publishing worker also receives its own messages through
Redis (one delivery path, one ordering), so `publish()` returning does not yet mean
local subscribers or `history()` have seen the envelope.

`history()` is synchronous in the interface, so it is served from the local ring buffer
of envelopes this process has seen: everything that arrived over pub/sub since start plus
the Redis log, which is loaded once per topic on first `subscribe()`. Gateways therefore
subscribe first and read `history()` right after.

Ordering: INCR and PUBLISH are separate commands, so two *workers* publishing on one topic
at the same instant may be delivered out of seq order; publishes within one process are
serialised per topic. If the reader loses its connection, every local subscription is
marked `lagged` so gateways ask clients to resync.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Any

from app.core.config import Settings
from app.realtime.bus import EventBus, Subscription
from app.realtime.events import Envelope

log = logging.getLogger(__name__)

LOG_TTL_SECONDS = 24 * 3600
RECONNECT_DELAY_SECONDS = 1.0


class RedisEventBus(EventBus):
    """`client` is a `redis.asyncio.Redis` (or a test double with the same async API)."""

    def __init__(self, client: Any, *, history_size: int = 500, prefix: str = "evently", owns_client: bool = False):
        self._client = client
        self._history_size = history_size
        self._prefix = prefix
        self._owns_client = owns_client
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._history: dict[str, deque[Envelope]] = defaultdict(lambda: deque(maxlen=history_size))
        self._seeded: set[str] = set()
        self._seed_lock = asyncio.Lock()
        self._publish_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pubsub: Any = None
        self._reader: asyncio.Task[None] | None = None

    # -- keys -----------------------------------------------------------------

    def _seq_key(self, topic: str) -> str:
        return f"{self._prefix}:seq:{topic}"

    def _log_key(self, topic: str) -> str:
        return f"{self._prefix}:log:{topic}"

    def _channel(self, topic: str) -> str:
        return f"{self._prefix}:bus:{topic}"

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Open the pub/sub connection and start the reader task."""
        if self._reader is not None:
            return
        self._pubsub = self._client.pubsub()
        await self._pubsub.psubscribe(self._channel("*"))
        self._reader = asyncio.create_task(self._read_loop(), name="redis-bus-reader")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort shutdown
                pass
            self._reader = None
        if self._pubsub is not None:
            await self._pubsub.aclose()
            self._pubsub = None
        if self._owns_client:
            await self._client.aclose()

    # -- EventBus -------------------------------------------------------------

    async def publish(
        self, topic: str, type: str, data: dict[str, Any] | None = None, *, retain: bool = True
    ) -> Envelope:
        if not retain:
            # Ephemeral: fan out only, no seq, no log (see EventBus.publish).
            envelope = Envelope(type=type, topic=topic, seq=0, data=data or {})
            await self._client.publish(self._channel(topic), envelope.model_dump_json())
            return envelope
        async with self._publish_locks[topic]:
            seq = int(await self._client.incr(self._seq_key(topic)))
            envelope = Envelope(type=type, topic=topic, seq=seq, data=data or {})
            payload = envelope.model_dump_json()
            pipe = self._client.pipeline(transaction=True)
            if self._history_size > 0:
                pipe.rpush(self._log_key(topic), payload)
                pipe.ltrim(self._log_key(topic), -self._history_size, -1)
                pipe.expire(self._log_key(topic), LOG_TTL_SECONDS)
            pipe.publish(self._channel(topic), payload)
            await pipe.execute()
        return envelope

    async def subscribe(self, topic: str, *, since_seq: int | None = None) -> Subscription:
        if self._reader is None:
            raise RuntimeError("RedisEventBus is not started")
        await self._seed(topic)
        # Nothing may await below: the caller relies on history() being consistent
        # with the subscription the moment this returns.
        sub = Subscription(self, topic)
        if since_seq is not None:
            for envelope in self.history(topic, since_seq):
                sub.push(envelope)
        self._subs[topic].add(sub)
        return sub

    async def unsubscribe(self, subscription: Subscription) -> None:
        subs = self._subs.get(subscription.topic)
        if subs:
            subs.discard(subscription)
            if not subs:
                self._subs.pop(subscription.topic, None)

    def history(self, topic: str, since_seq: int = 0) -> list[Envelope]:
        """Envelopes this process has seen for `topic` (ring buffer), newest last."""
        return [e for e in self._history.get(topic, ()) if e.seq > since_seq]

    def subscriber_count(self, topic: str) -> int:
        return len(self._subs.get(topic, ()))

    # -- internals ------------------------------------------------------------

    async def _seed(self, topic: str) -> None:
        """Load the Redis log into the local ring once per topic.

        Held under a lock so no subscriber exists for the topic while the ring is being
        back-filled; otherwise a live message deduplicated against the seed could be lost.
        """
        if topic in self._seeded or self._history_size == 0:
            return
        async with self._seed_lock:
            if topic in self._seeded:
                return
            for raw in await self._client.lrange(self._log_key(topic), 0, -1):
                envelope = self._decode(raw)
                if envelope is not None:
                    self._remember(envelope)
            self._seeded.add(topic)

    def _remember(self, envelope: Envelope) -> bool:
        """Insert into the topic ring in seq order; False if that seq was already there."""
        ring = self._history[envelope.topic]
        if not ring or envelope.seq > ring[-1].seq:
            ring.append(envelope)
            return True
        if any(e.seq == envelope.seq for e in ring):
            return False
        merged = sorted([*ring, envelope], key=lambda e: e.seq)
        ring.clear()
        ring.extend(merged)  # maxlen keeps the newest
        return True

    @staticmethod
    def _decode(raw: str | bytes) -> Envelope | None:
        try:
            return Envelope.model_validate_json(raw)
        except ValueError:
            log.warning("ignoring malformed envelope on redis bus: %r", raw[:200])
            return None

    def _deliver(self, envelope: Envelope) -> None:
        if envelope.seq != 0 and not self._remember(envelope):
            return  # already seen via the seed (seq 0 = ephemeral, never remembered)
        for sub in list(self._subs.get(envelope.topic, ())):
            sub.push(envelope)

    def _mark_lagged(self) -> None:
        for subs in self._subs.values():
            for sub in subs:
                sub.lagged = True

    async def _read_loop(self) -> None:
        while True:
            try:
                message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep serving; redis-py reconnects on the next call
                log.exception("redis bus reader failed; subscribers will be asked to resync")
                self._mark_lagged()
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            if message is None or message.get("type") not in ("message", "pmessage"):
                continue
            envelope = self._decode(message["data"])
            if envelope is not None:
                self._deliver(envelope)


async def build_redis_bus(settings: Settings) -> EventBus:
    """Connect to `settings.redis_url` and return a started bus."""
    if not settings.redis_url:
        raise ValueError("redis_url is not configured")
    import redis.asyncio as redis  # optional dependency

    client = redis.from_url(settings.redis_url, decode_responses=True)
    await client.ping()
    bus = RedisEventBus(client, history_size=settings.bus_history_size, owns_client=True)
    await bus.start()
    return bus
