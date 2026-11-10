"""Publish/subscribe bus for realtime envelopes.

`InMemoryEventBus` is the default (single process). The interface is small so
a Redis-backed bus can replace it for multi-worker deployments.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Any

from app.realtime.events import Envelope


class Subscription:
    """Async iterator of envelopes for one topic. Use as an async context manager.

    Slow consumers never block publishers: when the queue is full the oldest
    envelope is dropped and `lagged` is set, so the gateway can tell the client
    to resync.
    """

    def __init__(self, bus: EventBus, topic: str, maxsize: int = 1000) -> None:
        self._bus = bus
        self.topic = topic
        self.queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=maxsize)
        self.lagged = False
        self._closed = False

    def push(self, envelope: Envelope) -> None:
        if self._closed:
            return
        if self.queue.full():
            self.lagged = True
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race guard
                pass
        self.queue.put_nowait(envelope)

    async def get(self, timeout: float | None = None) -> Envelope:
        """Next envelope; raises `TimeoutError` if `timeout` seconds pass first."""
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Envelope:
        if self._closed:
            raise StopAsyncIteration
        return await self.queue.get()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._bus.unsubscribe(self)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class EventBus(ABC):
    @abstractmethod
    async def publish(
        self, topic: str, type: str, data: dict[str, Any] | None = None, *, retain: bool = True
    ) -> Envelope:
        """Publish an envelope to every subscriber of `topic` and return it.

        retain=False marks the envelope ephemeral (cursor presence, typing…): it is
        delivered live with `seq=0` but never numbered or kept for replay, so a
        chatty signal cannot push real state changes out of the replay buffer.
        Clients must not use a seq of 0 as a resume point.
        """

    @abstractmethod
    async def subscribe(self, topic: str, *, since_seq: int | None = None) -> Subscription:
        """Subscribe to `topic`. With `since_seq`, envelopes newer than it are replayed first."""

    @abstractmethod
    async def unsubscribe(self, subscription: Subscription) -> None: ...

    @abstractmethod
    def history(self, topic: str, since_seq: int = 0) -> list[Envelope]:
        """Retained envelopes for `topic` with seq > since_seq (bounded ring buffer)."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""


class InMemoryEventBus(EventBus):
    def __init__(self, history_size: int = 500) -> None:
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._history: dict[str, deque[Envelope]] = defaultdict(lambda: deque(maxlen=history_size))
        self._seq: dict[str, int] = defaultdict(int)

    async def publish(
        self, topic: str, type: str, data: dict[str, Any] | None = None, *, retain: bool = True
    ) -> Envelope:
        if retain:
            self._seq[topic] += 1
            envelope = Envelope(type=type, topic=topic, seq=self._seq[topic], data=data or {})
            self._history[topic].append(envelope)
        else:
            envelope = Envelope(type=type, topic=topic, seq=0, data=data or {})
        for sub in list(self._subs.get(topic, ())):
            sub.push(envelope)
        return envelope

    async def subscribe(self, topic: str, *, since_seq: int | None = None) -> Subscription:
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
        return [e for e in self._history.get(topic, ()) if e.seq > since_seq]

    def subscriber_count(self, topic: str) -> int:
        return len(self._subs.get(topic, ()))
