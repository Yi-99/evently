"""In-process doubles shared by the audio / STT / LiveKit tests (no tests in this module).

`FakeLive` stands in for `app.live.service` (Package 13) and `FakeProvider` for a streaming STT
provider, so the audio package is tested without the network or the other packages' code.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from app.live import service as live_service
from app.live.service import SessionClient, SessionNotFound
from app.transcription import factory
from app.transcription.base import (
    TranscriptEvent,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionStream,
)

SESSION_ID = "ses_test"
PCM_20MS = b"\x01\x00" * 320  # 20 ms of PCM16 mono at 16 kHz


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# --- live service double -------------------------------------------------------


@dataclass
class FakeLive:
    """Sessions, tokens and recorded transcript ingests. Install with `install()`."""

    sessions: dict[str, Any] = field(default_factory=dict)
    clients: dict[str, SessionClient] = field(default_factory=dict)
    keyterms: list[str] = field(default_factory=lambda: ["budget", "venue"])
    ingested: list[TranscriptEvent] = field(default_factory=list)

    def add_session(self, session_id: str = SESSION_ID, *, status: str = "live", **settings: Any) -> Any:
        session = SimpleNamespace(id=session_id, status=status, settings=settings)
        self.sessions[session_id] = session
        return session

    def add_client(self, token: str, role: str, *, participant_id: str | None = None, session_id: str = SESSION_ID):
        self.clients[token] = SessionClient(
            session_id=session_id,
            role=role,
            participant_id=participant_id,
            display_name=f"{role}-{token}",
            user_id="usr_owner" if participant_id is None else None,
        )

    async def authenticate_session_client(self, ctx, session_id: str, token: str | None) -> SessionClient | None:
        if session_id not in self.sessions:
            raise SessionNotFound(session_id)
        client = self.clients.get(token or "")
        return client if client is not None and client.session_id == session_id else None

    async def get_session(self, ctx, session_id: str) -> Any:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise SessionNotFound(session_id) from None

    async def get_keyterms(self, ctx, session_id: str) -> list[str]:
        return list(self.keyterms)

    async def ingest_transcript(self, ctx, session_id: str, event: TranscriptEvent, *, source: str = "stt") -> None:
        self.ingested.append(event)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeLive:
        for name in ("authenticate_session_client", "get_session", "get_keyterms", "ingest_transcript"):
            monkeypatch.setattr(live_service, name, getattr(self, name))
        return self


def standard_live(monkeypatch: pytest.MonkeyPatch, **settings: Any) -> FakeLive:
    """One live session with a host, a speaker and a listener token."""
    live = FakeLive().install(monkeypatch)
    live.add_session(**settings)
    live.add_client("host-token", "host", participant_id=None)
    live.add_client("speaker-token", "speaker", participant_id="prt_speaker")
    live.add_client("speaker2-token", "speaker", participant_id="prt_speaker2")
    live.add_client("listener-token", "listener", participant_id="prt_listener")
    return live


# --- transcription provider double -----------------------------------------------


class FakeStream(TranscriptionStream):
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.finished = False
        self.closed = False
        self.send_gate: asyncio.Event | None = None  # when set, send_audio blocks until it is set
        self._events: asyncio.Queue[TranscriptEvent | Exception | None] = asyncio.Queue()

    @property
    def received(self) -> bytes:
        return b"".join(self.audio)

    async def send_audio(self, chunk: bytes) -> None:
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.audio.append(chunk)

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self._events.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def finish(self) -> None:
        self.finished = True
        # Like a real provider: flush a final result, then close the stream.
        self._events.put_nowait(TranscriptEvent(text="(flushed)", is_final=True))
        self._events.put_nowait(None)

    async def close(self) -> None:
        self.closed = True
        self._events.put_nowait(None)

    def emit(self, text: str, *, is_final: bool = True) -> None:
        self._events.put_nowait(TranscriptEvent(text=text, is_final=is_final))

    def fail(self, message: str = "connection dropped") -> None:
        self._events.put_nowait(TranscriptionError(message))

    def end(self) -> None:
        """Provider closes the stream cleanly without being asked."""
        self._events.put_nowait(None)


class FakeProvider(TranscriptionProvider):
    name = "fake"

    def __init__(self, *, open_failures: int = 0) -> None:
        self.open_failures = open_failures  # how many open() calls fail before one succeeds
        self.opens = 0
        self.configs: list[TranscriptionConfig] = []
        self.streams: list[FakeStream] = []

    async def open(self, config: TranscriptionConfig) -> TranscriptionStream:
        self.opens += 1
        self.configs.append(config)
        if self.open_failures > 0:
            self.open_failures -= 1
            raise TranscriptionError("connect failed")
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
        monkeypatch.setattr(factory, "get_transcription_provider", lambda settings, name=None: self)
        return self


def install_browser_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "get_transcription_provider", lambda settings, name=None: None)
