"""Scripted transcription provider for tests and keyless development."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar

from app.transcription.base import (
    TranscriptEvent,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionStream,
)

Scripted = TranscriptEvent | str


def _as_event(item: Scripted) -> TranscriptEvent:
    return item if isinstance(item, TranscriptEvent) else TranscriptEvent(text=item, is_final=True)


class MockTranscriptionProvider(TranscriptionProvider):
    """Plays a script of `TranscriptEvent`s as audio arrives.

    `script` items are `TranscriptEvent`s or plain strings (-> final events). One item is
    emitted per `chunks_per_event` audio chunks; whatever is left is flushed on `finish()`.
    With no script the stream emits nothing. When `script` is None the class-level
    `default_script` is used, so a test that drives the app (which constructs the provider
    itself) can pre-load it. Every opened stream is kept in `streams` and in the class-level
    `last_stream`; each records its `config` and the audio chunks it `received`.
    """

    name = "mock"
    default_script: ClassVar[list[Scripted]] = []
    last_stream: ClassVar[MockStream | None] = None

    def __init__(self, script: list[Scripted] | None = None, *, chunks_per_event: int = 1) -> None:
        if chunks_per_event < 1:
            raise ValueError("chunks_per_event must be >= 1")
        self.script: list[Scripted] = list(self.default_script if script is None else script)
        self.chunks_per_event = chunks_per_event
        self.streams: list[MockStream] = []

    async def open(self, config: TranscriptionConfig) -> TranscriptionStream:
        stream = MockStream(config, [_as_event(item) for item in self.script], self.chunks_per_event)
        self.streams.append(stream)
        type(self).last_stream = stream
        return stream


class MockStream(TranscriptionStream):
    def __init__(self, config: TranscriptionConfig, script: list[TranscriptEvent], chunks_per_event: int) -> None:
        self.config = config
        self.received: list[bytes] = []
        self.finished = False
        self.closed = False
        self._script = list(script)
        self._chunks_per_event = chunks_per_event
        self._queue: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()

    @property
    def received_bytes(self) -> bytes:
        return b"".join(self.received)

    async def send_audio(self, chunk: bytes) -> None:
        if self.closed:
            raise TranscriptionError("transcription stream is closed")
        self.received.append(chunk)
        if self._script and len(self.received) % self._chunks_per_event == 0:
            self._queue.put_nowait(self._script.pop(0))

    def events(self) -> AsyncIterator[TranscriptEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def finish(self) -> None:
        if self.closed:
            return
        self.finished = True
        while self._script:
            self._queue.put_nowait(self._script.pop(0))
        await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._queue.put_nowait(None)
