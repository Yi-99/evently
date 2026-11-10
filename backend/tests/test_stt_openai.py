"""OpenAI realtime transcription provider against an in-process fake server."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import struct
from collections.abc import AsyncIterator

import pytest
from websockets.asyncio.server import ServerConnection, serve

from app.transcription.base import TranscriptEvent, TranscriptionConfig, TranscriptionError
from app.transcription.openai_realtime import OpenAIRealtimeProvider

SR = 16000  # what callers send


def pcm(ms: int, amplitude: int, freq: float = 440.0, rate: int = SR) -> bytes:
    n = rate * ms // 1000
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))


LOUD = pcm(40, 8000)
QUIET = pcm(40, 0)


class FakeOpenAI:
    """Speaks just enough of the realtime transcription protocol for the provider.

    Acknowledges `session.update`, answers each commit with `input_audio_buffer.committed`
    and then plays `on_commit(item_id)` events. Tests push arbitrary server events with
    `send()`. Everything the client sent is kept in `received`.
    """

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.paths: list[str] = []
        self.auth: list[str | None] = []
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.reject_update = False
        self.on_commit = lambda item_id: [
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": item_id, "transcript": "done"}
        ]
        self._ws: ServerConnection | None = None
        self._commits = 0

    @property
    def appended_bytes(self) -> bytes:
        return b"".join(base64.b64decode(m["audio"]) for m in self.received if m["type"] == "input_audio_buffer.append")

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.received if m["type"] == kind]

    async def send(self, event: dict) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(event))

    async def drop(self) -> None:
        assert self._ws is not None
        await self._ws.close(code=1011, reason="server going away")

    async def handler(self, ws: ServerConnection) -> None:
        self._ws = ws
        self.paths.append(ws.request.path)
        self.auth.append(ws.request.headers.get("Authorization"))
        await ws.send(json.dumps({"type": "session.created", "session": {"type": "transcription"}}))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                if msg["type"] == "session.update":
                    if self.reject_update:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "error": {"type": "invalid_request_error", "code": "bad_param", "message": "nope"},
                                }
                            )
                        )
                    else:
                        await ws.send(json.dumps({"type": "session.updated", "session": msg["session"]}))
                        self.connected.set()
                elif msg["type"] == "input_audio_buffer.commit":
                    self._commits += 1
                    item_id = f"item_{self._commits}"
                    await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": item_id}))
                    for event in self.on_commit(item_id):
                        await ws.send(json.dumps(event))
        finally:
            self.closed.set()


@contextlib.asynccontextmanager
async def fake_server(**serve_kwargs) -> AsyncIterator[tuple[FakeOpenAI, str]]:
    fake = FakeOpenAI()
    async with serve(fake.handler, "127.0.0.1", 0, **serve_kwargs) as server:
        port = server.sockets[0].getsockname()[1]
        yield fake, f"ws://127.0.0.1:{port}/v1/realtime?intent=transcription"


async def collect(stream, n: int, timeout: float = 5.0) -> list[TranscriptEvent]:
    out: list[TranscriptEvent] = []

    async def pull() -> None:
        async for ev in stream.events():
            out.append(ev)
            if len(out) >= n:
                return

    await asyncio.wait_for(pull(), timeout)
    return out


async def wait_until(predicate, timeout: float = 5.0) -> None:
    async def loop() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


# -- handshake -----------------------------------------------------------------


async def test_connects_with_bearer_auth_and_sends_session_config():
    async with fake_server() as (fake, url):
        provider = OpenAIRealtimeProvider("sk-test", url=url)
        config = TranscriptionConfig(language="en-US", keyterms=["Evently", " next  slide ", "", "bad<tag>", "Evently"])
        stream = await provider.open(config)
        try:
            assert fake.paths == ["/v1/realtime?intent=transcription"]
            assert "model=" not in fake.paths[0]
            assert fake.auth == ["Bearer sk-test"]
            update = fake.of_type("session.update")[0]
            session = update["session"]
            assert session["type"] == "transcription"
            audio_in = session["audio"]["input"]
            assert audio_in["format"] == {"type": "audio/pcm", "rate": 24000}
            assert audio_in["turn_detection"] is None
            assert audio_in["noise_reduction"] == {"type": "near_field"}
            assert audio_in["transcription"] == {
                "model": "gpt-live-transcribe",
                "languages": ["en"],
                "keywords": ["Evently", "next slide"],
                "delay": "low",
            }
        finally:
            await stream.close()


async def test_open_fails_on_http_401():
    async def reject(connection, request):
        return connection.respond(401, "Unauthorized\n")

    async with fake_server(process_request=reject) as (_fake, url):
        with pytest.raises(TranscriptionError, match="401"):
            await OpenAIRealtimeProvider("sk-bad", url=url).open(TranscriptionConfig())


async def test_open_fails_when_server_rejects_session_update():
    async with fake_server() as (fake, url):
        fake.reject_update = True
        with pytest.raises(TranscriptionError, match="nope"):
            await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        await asyncio.wait_for(fake.closed.wait(), 5)


async def test_open_fails_when_nothing_listens():
    with pytest.raises(TranscriptionError, match="connect failed"):
        await OpenAIRealtimeProvider("sk-test", url="ws://127.0.0.1:9/", connect_timeout=2).open(TranscriptionConfig())


# -- audio path -----------------------------------------------------------------


async def test_audio_is_resampled_to_24k_and_appended():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig(sample_rate=SR))
        try:
            chunk = QUIET  # 40 ms = 640 samples at 16 kHz
            await stream.send_audio(chunk[:333])  # odd split: sample boundaries must be preserved
            await stream.send_audio(chunk[333:])
            await wait_until(lambda: len(fake.of_type("input_audio_buffer.append")) == 2)
            appended = fake.appended_bytes
            assert len(appended) % 2 == 0
            # 640 source samples -> 960 at 24 kHz, minus the trailing sample held for continuity.
            assert abs(len(appended) // 2 - 960) <= 1
            assert not fake.of_type("input_audio_buffer.commit")
        finally:
            await stream.close()


async def test_speech_then_silence_commits_a_turn():
    async with fake_server() as (fake, url):
        provider = OpenAIRealtimeProvider("sk-test", url=url, silence_ms=200)
        stream = await provider.open(TranscriptionConfig(sample_rate=SR))
        try:
            for _ in range(10):
                await stream.send_audio(LOUD)  # 400 ms of speech
            assert not fake.of_type("input_audio_buffer.commit")
            for _ in range(5):
                await stream.send_audio(QUIET)  # 200 ms of silence
            await wait_until(lambda: len(fake.of_type("input_audio_buffer.commit")) == 1)
            final = (await collect(stream, 1))[0]
            assert final == TranscriptEvent(text="done", is_final=True, start_ms=0, end_ms=600)
        finally:
            await stream.close()


async def test_long_turn_is_committed_at_max_turn():
    async with fake_server() as (fake, url):
        provider = OpenAIRealtimeProvider("sk-test", url=url, max_turn_ms=400)
        stream = await provider.open(TranscriptionConfig(sample_rate=SR))
        try:
            for _ in range(20):
                await stream.send_audio(LOUD)  # 800 ms without a pause
            await wait_until(lambda: len(fake.of_type("input_audio_buffer.commit")) == 2)
        finally:
            await stream.close()


# -- transcript events ---------------------------------------------------------


async def test_deltas_become_cumulative_interims_and_completed_one_final():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        try:
            delta = "conversation.item.input_audio_transcription.delta"
            await fake.send({"type": delta, "item_id": "item_a", "delta": "Hello"})
            await fake.send({"type": delta, "item_id": "item_a", "delta": ", next"})
            await fake.send({"type": delta, "item_id": "item_a", "delta": " slide"})
            await fake.send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item_a",
                    "transcript": "Hello, next slide.",
                }
            )
            # Empty finals (silence-only turns) are dropped.
            await fake.send(
                {"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_b", "transcript": ""}
            )
            await fake.send({"type": delta, "item_id": "item_c", "delta": "Thanks"})
            got = await collect(stream, 5)
            assert [(e.text, e.is_final) for e in got] == [
                ("Hello", False),
                ("Hello, next", False),
                ("Hello, next slide", False),
                ("Hello, next slide.", True),
                ("Thanks", False),
            ]
        finally:
            await stream.close()


async def test_interims_can_be_disabled():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig(interim_results=False))
        try:
            await fake.send(
                {"type": "conversation.item.input_audio_transcription.delta", "item_id": "i", "delta": "Hi"}
            )
            await fake.send(
                {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i", "transcript": "Hi."}
            )
            assert (await collect(stream, 1)) == [TranscriptEvent(text="Hi.", is_final=True)]
        finally:
            await stream.close()


async def test_failed_turn_is_skipped_and_stream_continues():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        try:
            await fake.send(
                {
                    "type": "conversation.item.input_audio_transcription.failed",
                    "item_id": "x",
                    "error": {"message": "transient"},
                }
            )
            await fake.send(
                {"type": "conversation.item.input_audio_transcription.completed", "item_id": "y", "transcript": "ok"}
            )
            assert (await collect(stream, 1))[0].text == "ok"
        finally:
            await stream.close()


# -- failures ------------------------------------------------------------------


async def test_error_event_fails_the_stream():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        await fake.send({"type": "error", "error": {"type": "server_error", "code": "boom", "message": "exploded"}})
        with pytest.raises(TranscriptionError, match="exploded"):
            await collect(stream, 1)
        with pytest.raises(TranscriptionError, match="exploded"):
            await stream.send_audio(QUIET)
        await stream.close()
        await stream.close()  # idempotent


async def test_dropped_connection_fails_the_stream():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        await fake.drop()
        with pytest.raises(TranscriptionError, match="dropped"):
            await collect(stream, 1)
        await stream.close()


async def test_send_after_close_raises():
    async with fake_server() as (_fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        await stream.close()
        with pytest.raises(TranscriptionError, match="closed"):
            await stream.send_audio(QUIET)
        assert [e async for e in stream.events()] == []


# -- finish / close --------------------------------------------------------------


async def test_finish_commits_tail_drains_final_and_ends_events():
    async with fake_server() as (fake, url):

        def slow_final(item_id: str) -> list[dict]:
            return [
                {"type": "conversation.item.input_audio_transcription.delta", "item_id": item_id, "delta": "bye"},
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": item_id,
                    "transcript": "Goodbye.",
                },
            ]

        fake.on_commit = slow_final
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig(sample_rate=SR))
        for _ in range(5):
            await stream.send_audio(LOUD)  # 200 ms, no pause: nothing committed yet
        assert not fake.of_type("input_audio_buffer.commit")
        await asyncio.wait_for(stream.finish(), 5)
        assert len(fake.of_type("input_audio_buffer.commit")) == 1
        got = [e async for e in stream.events()]
        assert [(e.text, e.is_final) for e in got] == [("bye", False), ("Goodbye.", True)]
        assert got[1].start_ms == 0 and got[1].end_ms == 200
        assert stream._reader.done()  # no leaked task
        await asyncio.wait_for(fake.closed.wait(), 5)


async def test_finish_without_audio_just_closes():
    async with fake_server() as (fake, url):
        stream = await OpenAIRealtimeProvider("sk-test", url=url).open(TranscriptionConfig())
        await asyncio.wait_for(stream.finish(), 5)
        assert not fake.of_type("input_audio_buffer.commit")
        assert [e async for e in stream.events()] == []


async def test_finish_times_out_when_final_never_arrives():
    async with fake_server() as (fake, url):
        fake.on_commit = lambda item_id: []
        provider = OpenAIRealtimeProvider("sk-test", url=url, finish_timeout=0.2)
        stream = await provider.open(TranscriptionConfig(sample_rate=SR))
        for _ in range(5):
            await stream.send_audio(LOUD)
        await asyncio.wait_for(stream.finish(), 5)
        assert [e async for e in stream.events()] == []
