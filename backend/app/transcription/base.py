"""Streaming speech-to-text contracts.

A provider opens a `TranscriptionStream`; the caller pushes raw audio with
`send_audio()` and consumes `TranscriptEvent`s from `events()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


class TranscriptionError(Exception):
    """The STT provider failed (bad credentials, dropped connection, protocol error)."""


@dataclass
class TranscriptionConfig:
    sample_rate: int = 16000
    encoding: str = "pcm16"  # PCM signed 16-bit little-endian, mono
    language: str = "en"
    interim_results: bool = True
    # Words/phrases to bias recognition toward (slide trigger keywords, names).
    keyterms: list[str] = field(default_factory=list)


@dataclass
class TranscriptEvent:
    text: str
    is_final: bool
    start_ms: int | None = None
    end_ms: int | None = None
    confidence: float | None = None
    speaker: str | None = None


class TranscriptionStream(ABC):
    @abstractmethod
    async def send_audio(self, chunk: bytes) -> None:
        """Push one chunk of audio in the configured encoding."""

    @abstractmethod
    def events(self) -> AsyncIterator[TranscriptEvent]:
        """Interim and final results. Ends when the stream closes; raises `TranscriptionError` on failure."""

    @abstractmethod
    async def finish(self) -> None:
        """Signal end of audio and let the provider flush remaining results."""

    @abstractmethod
    async def close(self) -> None:
        """Tear the connection down immediately. Safe to call more than once."""


class TranscriptionProvider(ABC):
    #: Registry name: deepgram | assemblyai | openai | mock
    name: str = ""

    @abstractmethod
    async def open(self, config: TranscriptionConfig) -> TranscriptionStream:
        """Connect and return a ready stream. Raises `TranscriptionError` if it cannot."""
