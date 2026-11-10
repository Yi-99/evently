"""Scripted mock transcription provider."""

from __future__ import annotations

import asyncio

import pytest

from app.transcription.base import TranscriptEvent, TranscriptionConfig, TranscriptionError
from app.transcription.mock import MockTranscriptionProvider


async def drain(stream) -> list[TranscriptEvent]:
    return await asyncio.wait_for(_collect(stream), 5)


async def _collect(stream) -> list[TranscriptEvent]:
    return [e async for e in stream.events()]


async def test_script_plays_one_event_per_chunk_and_flushes_on_finish():
    provider = MockTranscriptionProvider(["hello", TranscriptEvent("partial", is_final=False), "world"])
    stream = await provider.open(TranscriptionConfig(keyterms=["x"]))
    await stream.send_audio(b"\x00\x01")
    await stream.send_audio(b"\x02\x03")
    await stream.finish()  # the third item is flushed here
    got = await drain(stream)
    assert [(e.text, e.is_final) for e in got] == [("hello", True), ("partial", False), ("world", True)]
    assert stream.received == [b"\x00\x01", b"\x02\x03"] and stream.received_bytes == b"\x00\x01\x02\x03"
    assert stream.config.keyterms == ["x"] and stream.finished and stream.closed
    assert provider.streams == [stream] and MockTranscriptionProvider.last_stream is stream


async def test_chunks_per_event_spacing():
    provider = MockTranscriptionProvider(["a", "b"], chunks_per_event=3)
    stream = await provider.open(TranscriptionConfig())
    for _ in range(5):
        await stream.send_audio(b"..")
    assert stream._queue.qsize() == 1  # only "a" so far (after chunk 3)
    await stream.send_audio(b"..")
    await stream.close()
    assert [e.text for e in await drain(stream)] == ["a", "b"]


async def test_no_script_emits_nothing():
    stream = await MockTranscriptionProvider().open(TranscriptionConfig())
    await stream.send_audio(b"..")
    await stream.finish()
    assert await drain(stream) == []


async def test_default_script_is_used_when_none_given(monkeypatch):
    monkeypatch.setattr(MockTranscriptionProvider, "default_script", ["from default"])
    stream = await MockTranscriptionProvider().open(TranscriptionConfig())
    await stream.send_audio(b"..")
    await stream.close()
    assert [e.text for e in await drain(stream)] == ["from default"]
    # An explicit (even empty) script wins over the default.
    stream = await MockTranscriptionProvider([]).open(TranscriptionConfig())
    await stream.finish()
    assert await drain(stream) == []


async def test_close_is_idempotent_and_send_after_close_raises():
    stream = await MockTranscriptionProvider(["x"]).open(TranscriptionConfig())
    await stream.close()
    await stream.close()
    await stream.finish()
    with pytest.raises(TranscriptionError):
        await stream.send_audio(b"..")
    assert await drain(stream) == []


def test_rejects_bad_spacing():
    with pytest.raises(ValueError):
        MockTranscriptionProvider(chunks_per_event=0)
