"""SttPipeline: provider resolution, audio forwarding, transcript ingestion, reconnects, cleanup."""

from __future__ import annotations

import asyncio

import pytest

from app.live import service as live_service
from app.live.audio_hub import AudioFormat
from app.live.stt_pipeline import SttPipeline, get_stt_pipeline
from app.realtime import events as ev
from app.schemas.live import SessionSettings
from app.transcription import factory
from app.transcription.base import TranscriptionError
from tests.helpers import session_published
from tests.test_audio_fakes import PCM_20MS, SESSION_ID, FakeLive, FakeProvider, install_browser_mode, wait_until

PCM16K = AudioFormat(codec="pcm16", sample_rate=16000, frame_ms=20)
SETTINGS = SessionSettings(stt_provider="fake", language="de")


@pytest.fixture
def live(monkeypatch) -> FakeLive:
    return FakeLive().install(monkeypatch)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    return FakeProvider().install(monkeypatch)


def pipeline_for(ctx, **kwargs) -> SttPipeline:
    kwargs.setdefault("max_reconnects", 2)
    kwargs.setdefault("backoff_base_s", 0.01)
    kwargs.setdefault("backoff_max_s", 0.02)
    return SttPipeline(ctx, **kwargs)


def stt_states(ctx) -> list[tuple[str, str, str | None]]:
    return [
        (e.data["provider"], e.data["status"], e.data["error"])
        for e in session_published(ctx, SESSION_ID, ev.SESSION_STT_STATE)
    ]


def stt_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("stt:")]


def feed_ms(session, ms: int) -> bytes:
    data = b""
    for _ in range(ms // 20):
        session.feed(PCM_20MS)
        data += PCM_20MS
    return data


async def test_audio_and_events_flow_through_with_keyterms(ctx, live, provider):
    pipeline = pipeline_for(ctx)
    session = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    assert session is not None and pipeline.active(SESSION_ID) is session
    await wait_until(lambda: provider.streams)
    stream = provider.streams[0]
    config = provider.configs[0]
    assert (config.sample_rate, config.encoding, config.language, config.keyterms) == (
        16000,
        "pcm16",
        "de",
        ["budget", "venue"],
    )
    assert stt_states(ctx) == [("fake", "connecting", None), ("fake", "active", None)]

    sent = feed_ms(session, 200)
    await wait_until(lambda: stream.received == sent)
    # Small speaker frames are coalesced to >= 100 ms before reaching the provider.
    assert all(len(chunk) >= 3200 for chunk in stream.audio)

    stream.emit("hello", is_final=False)
    stream.emit("hello world")
    await wait_until(lambda: len(live.ingested) == 2)
    assert [(e.text, e.is_final) for e in live.ingested] == [("hello", False), ("hello world", True)]

    session.feed(PCM_20MS)  # a trailing partial chunk must be flushed on stop
    await session.stop()
    assert stream.finished and stream.closed and stream.received == sent + PCM_20MS
    assert live.ingested[-1].text == "(flushed)"  # the provider's final result after finish() still arrives
    assert stt_states(ctx)[-1] == ("fake", "stopped", None)
    assert pipeline.active(SESSION_ID) is None and not stt_tasks()
    await session.stop()  # idempotent
    assert stt_states(ctx).count(("fake", "stopped", None)) == 1


async def test_browser_mode_is_a_server_side_noop(ctx, live, monkeypatch):
    install_browser_mode(monkeypatch)
    pipeline = pipeline_for(ctx)
    assert await pipeline.start(SESSION_ID, PCM16K, SessionSettings(stt_provider="browser")) is None
    assert stt_states(ctx) == [] and pipeline.active(SESSION_ID) is None and not stt_tasks()


async def test_non_pcm_codec_publishes_a_clear_error_state(ctx, live, provider):
    pipeline = pipeline_for(ctx)
    assert await pipeline.start(SESSION_ID, AudioFormat(codec="opus", sample_rate=48000), SETTINGS) is None
    (state,) = stt_states(ctx)
    assert state[:2] == ("fake", "error") and "pcm16" in state[2] and "opus" in state[2]
    assert provider.opens == 0 and not stt_tasks()


async def test_unusable_provider_publishes_error_and_leaves_audio_alone(ctx, live, monkeypatch):
    def no_key(settings, name=None):
        raise TranscriptionError("deepgram: DEEPGRAM_API_KEY is not set")

    monkeypatch.setattr(factory, "get_transcription_provider", no_key)
    pipeline = pipeline_for(ctx)
    assert await pipeline.start(SESSION_ID, PCM16K, SessionSettings(stt_provider="deepgram")) is None
    assert stt_states(ctx) == [("deepgram", "error", "deepgram: DEEPGRAM_API_KEY is not set")]


async def test_connect_failures_reconnect_with_backoff_then_give_up(ctx, live, monkeypatch):
    provider = FakeProvider(open_failures=10).install(monkeypatch)
    pipeline = pipeline_for(ctx, max_reconnects=2)
    session = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: session.status == "error")
    assert provider.opens == 3 and session.reconnects == 2
    assert [s[1] for s in stt_states(ctx)] == ["connecting", "connecting", "connecting", "error"]
    assert stt_states(ctx)[-1][2] == "connect failed"
    assert stt_states(ctx)[1][2] == "connect failed"  # reconnect states carry the last error
    session.feed(PCM_20MS)  # harmless after giving up
    await session.stop()
    assert stt_states(ctx)[-1][1] == "error"  # no misleading "stopped" after an error
    assert pipeline.active(SESSION_ID) is None and not stt_tasks()


async def test_mid_stream_failure_reconnects_and_keeps_transcribing(ctx, live, provider):
    pipeline = pipeline_for(ctx, max_reconnects=1)
    session = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: len(provider.streams) == 1)
    provider.streams[0].emit("first")
    await wait_until(lambda: len(live.ingested) == 1)

    provider.streams[0].fail("socket closed")
    await wait_until(lambda: len(provider.streams) == 2 and session.status == "active")
    assert provider.streams[0].closed
    assert [s[1] for s in stt_states(ctx)] == ["connecting", "active", "connecting", "active"]
    assert stt_states(ctx)[2][2] == "socket closed"

    sent = feed_ms(session, 100)
    await wait_until(lambda: provider.streams[1].received == sent)
    provider.streams[1].emit("second")
    await wait_until(lambda: len(live.ingested) == 2)

    # A healthy stream (it produced results) resets the budget: this second loss is survived too.
    provider.streams[1].end()
    await wait_until(lambda: len(provider.streams) == 3 and session.status == "active")
    assert "ended unexpectedly" in stt_states(ctx)[4][2]
    await session.stop()
    assert session.status == "stopped" and not stt_tasks()


async def test_slow_provider_drops_audio_for_stt_only(ctx, live, provider):
    pipeline = pipeline_for(ctx)
    session = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: provider.streams)
    stream = provider.streams[0]
    stream.send_gate = asyncio.Event()  # the provider stops accepting audio
    feed_ms(session, 5000)  # five seconds of audio; feed() never blocked
    assert session.audio_dropped > 0
    assert session._queue.buffered <= 2000 * PCM16K.bytes_per_ms
    stream.send_gate.set()
    await session.stop()
    assert len(stream.received) <= int(2100 * PCM16K.bytes_per_ms)  # only the freshest audio was transcribed


async def test_ingest_failure_does_not_break_the_stream(ctx, live, provider, monkeypatch):
    calls = []

    async def flaky_ingest(ctx_, session_id, event, *, source="stt"):
        calls.append(event.text)
        if len(calls) == 1:
            raise RuntimeError("db hiccup")

    monkeypatch.setattr(live_service, "ingest_transcript", flaky_ingest)
    pipeline = pipeline_for(ctx)
    session = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: provider.streams)
    provider.streams[0].emit("one")
    provider.streams[0].emit("two")
    await wait_until(lambda: calls == ["one", "two"])
    assert session.status == "active" and provider.opens == 1
    await session.stop()


async def test_keyterm_lookup_failure_degrades_to_no_keyterms(ctx, live, provider, monkeypatch):
    async def boom(ctx_, session_id):
        raise RuntimeError("deck unavailable")

    monkeypatch.setattr(live_service, "get_keyterms", boom)
    session = await pipeline_for(ctx).start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: provider.configs)
    assert provider.configs[0].keyterms == []
    await session.stop()


async def test_restart_for_a_session_replaces_the_running_transcription(ctx, live, provider):
    pipeline = pipeline_for(ctx)
    first = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: len(provider.streams) == 1)
    second = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    assert first.status == "stopped" and provider.streams[0].finished
    await wait_until(lambda: len(provider.streams) == 2)
    assert pipeline.active(SESSION_ID) is second
    await first.stop()  # the old connection's cleanup is a no-op now
    assert pipeline.active(SESSION_ID) is second and len(stt_tasks()) == 1
    await second.stop()


async def test_stop_is_bounded_when_the_provider_never_flushes(ctx, live, provider):
    pipeline = pipeline_for(ctx)
    session = await pipeline.start(SESSION_ID, PCM16K, SETTINGS)
    await wait_until(lambda: provider.streams)
    stream = provider.streams[0]
    stream.send_gate = asyncio.Event()  # finish() will never be reached
    session.feed(PCM_20MS)
    await asyncio.wait_for(session.stop(grace=0.1), 1.0)
    assert stream.closed and not stream.finished and not stt_tasks()
    assert stt_states(ctx)[-1][1] == "stopped"


async def test_pipeline_close_stops_everything(ctx, live, provider):
    pipeline = pipeline_for(ctx)
    sessions = [await pipeline.start(f"ses_{i}", PCM16K, SETTINGS) for i in range(3)]
    await wait_until(lambda: len(provider.streams) == 3)
    await pipeline.close()
    assert all(s.status == "stopped" for s in sessions) and all(s.closed for s in provider.streams)
    assert not stt_tasks() and all(pipeline.active(f"ses_{i}") is None for i in range(3))


async def test_get_stt_pipeline_is_an_app_singleton_with_a_shutdown_hook(ctx):
    pipeline = get_stt_pipeline(ctx)
    assert get_stt_pipeline(ctx) is pipeline
    assert any(getattr(hook, "__self__", None) is pipeline for hook in ctx._shutdown_hooks)
