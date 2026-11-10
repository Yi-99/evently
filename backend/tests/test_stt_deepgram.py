"""Deepgram streaming provider against a fake `/v1/listen` server on 127.0.0.1 (no network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from websockets.asyncio.server import ServerConnection, serve

from app.transcription import deepgram
from app.transcription.base import TranscriptEvent, TranscriptionConfig, TranscriptionError
from app.transcription.deepgram import DeepgramProvider, DeepgramStream

Script = Callable[["FakeDeepgram", ServerConnection], Awaitable[None]]


def results(text: str, *, is_final: bool, start: float = 0.0, duration: float = 1.0, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "type": "Results",
            "channel_index": [0, 1],
            "start": start,
            "duration": duration,
            "is_final": is_final,
            "speech_final": is_final,
            "channel": {"alternatives": [{"transcript": text, "confidence": confidence, "words": []}]},
            "metadata": {"request_id": "r", "model_info": {}, "model_uuid": "m"},
        }
    )


METADATA = json.dumps({"type": "Metadata", "request_id": "r", "duration": 1.0, "channels": 1})


class FakeDeepgram:
    """Records the upgrade request and every frame; `script` drives what the server sends back."""

    def __init__(self) -> None:
        self.url = ""
        self.query: dict[str, list[str]] = {}
        self.headers = None  # websockets Headers (case-insensitive)
        self.frames: list[bytes | dict] = []
        self.got_frame = asyncio.Event()
        self.reject_status: HTTPStatus | None = None
        self.script: Script | None = None

    async def process_request(self, connection: ServerConnection, request):
        if self.reject_status is not None:
            response = connection.respond(self.reject_status, "nope\n")
            response.headers["dg-error"] = "Invalid credentials."
            return response
        return None

    async def handler(self, connection: ServerConnection) -> None:
        self.query = parse_qs(urlsplit(connection.request.path).query)
        self.headers = connection.request.headers
        if self.script is not None:
            await self.script(self, connection)
            return
        await self.record(connection)

    async def record(self, connection: ServerConnection) -> None:
        async for frame in connection:
            self.frames.append(frame if isinstance(frame, bytes) else json.loads(frame))
            self.got_frame.set()

    def texts(self) -> list[dict]:
        return [f for f in self.frames if isinstance(f, dict)]

    def audio(self) -> list[bytes]:
        return [f for f in self.frames if isinstance(f, bytes)]


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[FakeDeepgram]:
    server = FakeDeepgram()
    async with serve(server.handler, "127.0.0.1", 0, process_request=server.process_request) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        server.url = f"ws://127.0.0.1:{port}/v1/listen"
        yield server


def provider(fake: FakeDeepgram, **kwargs) -> DeepgramProvider:
    kwargs.setdefault("keepalive_interval", 0.05)
    kwargs.setdefault("finish_timeout", 0.5)
    return DeepgramProvider("dg-secret", url=fake.url, **kwargs)


async def collect(stream) -> list[TranscriptEvent]:
    return [event async for event in stream.events()]


def our_tasks() -> set[asyncio.Task]:
    return {t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("deepgram-")}


async def test_connect_builds_url_and_auth_header(fake: FakeDeepgram):
    config = TranscriptionConfig(
        sample_rate=24000,
        language="en-US",
        interim_results=True,
        keyterms=["next slide", "Evently", "  next   slide ", "", "Q3 roadmap"],
    )
    stream = await provider(fake).open(config)
    await stream.close()

    assert fake.headers["Authorization"] == "Token dg-secret"
    assert fake.query["model"] == ["nova-3"]
    assert fake.query["language"] == ["en-US"]
    assert fake.query["encoding"] == ["linear16"]
    assert fake.query["sample_rate"] == ["24000"]
    assert fake.query["channels"] == ["1"]
    assert fake.query["interim_results"] == ["true"]
    assert fake.query["punctuate"] == ["true"]
    assert fake.query["keyterm"] == ["next slide", "Evently", "Q3 roadmap"]
    assert "endpointing" not in fake.query and "smart_format" not in fake.query


async def test_optional_params_and_interim_off(fake: FakeDeepgram):
    stream = await provider(fake, model="nova-3-general", endpointing_ms=300).open(
        TranscriptionConfig(interim_results=False)
    )
    await stream.close()
    assert fake.query["model"] == ["nova-3-general"]
    assert fake.query["interim_results"] == ["false"]
    assert fake.query["endpointing"] == ["300"]
    assert "keyterm" not in fake.query


def test_select_keyterms_respects_budget():
    assert deepgram.select_keyterms(["a", "A", " b  c "]) == ["a", "b c"]
    many = [f"term{i}" for i in range(500)]
    chosen = deepgram.select_keyterms(many)
    assert len(chosen) <= deepgram.KEYTERM_MAX_COUNT
    long_terms = [chr(ord("a") + i) * 400 for i in range(6)]  # ~100 estimated tokens each
    assert len(deepgram.select_keyterms(long_terms)) == 5  # stops at the 500-token budget
    assert deepgram.select_keyterms(["x" * 400] * 10) == ["x" * 400]  # de-duplicated before budgeting


async def test_rejects_non_pcm16_config(fake: FakeDeepgram):
    with pytest.raises(TranscriptionError, match="pcm16"):
        await provider(fake).open(TranscriptionConfig(encoding="opus"))


async def test_interim_then_final_mapping_without_duplicates(fake: FakeDeepgram):
    async def script(server: FakeDeepgram, ws: ServerConnection) -> None:
        await ws.send(results("next", is_final=False, start=0.0, duration=0.5))
        await ws.send(results("next sl", is_final=False, start=0.0, duration=0.9))
        await ws.send(results("", is_final=False))  # silence: skipped
        await ws.send(results("Next slide.", is_final=True, start=0.0, duration=1.2, confidence=0.97))
        await ws.send(METADATA)
        await ws.send(json.dumps({"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": 1.2}))
        await ws.send(results("thank", is_final=False, start=1.2, duration=0.4))
        await ws.send(results("Thank you.", is_final=True, start=1.2, duration=0.8, confidence=0.8))
        await ws.send(results("   ", is_final=True))  # empty flush: skipped
        await ws.close(1000)

    fake.script = script
    stream = await provider(fake).open(TranscriptionConfig())
    await stream.finish()
    events = await collect(stream)

    assert [(e.text, e.is_final) for e in events] == [
        ("next", False),
        ("next sl", False),
        ("Next slide.", True),
        ("thank", False),
        ("Thank you.", True),
    ]
    final = events[2]
    assert (final.start_ms, final.end_ms, final.confidence) == (0, 1200, 0.97)
    assert (events[4].start_ms, events[4].end_ms) == (1200, 2000)
    assert [e.text for e in events if e.is_final] == ["Next slide.", "Thank you."]


async def test_audio_forwarded_and_keepalive_during_silence(fake: FakeDeepgram):
    stream = await provider(fake, keepalive_interval=0.05).open(TranscriptionConfig())
    await stream.send_audio(b"\x01\x02" * 160)
    await stream.send_audio(b"")  # never forwarded: an empty frame means end-of-stream to Deepgram
    await asyncio.sleep(0.3)
    await stream.close()

    assert fake.audio() == [b"\x01\x02" * 160]
    keepalives = [f for f in fake.texts() if f == {"type": "KeepAlive"}]
    assert len(keepalives) >= 2
    assert {"type": "CloseStream"} not in fake.texts()


async def test_finish_sends_close_stream_after_audio_and_drains(fake: FakeDeepgram):
    async def script(server: FakeDeepgram, ws: ServerConnection) -> None:
        async for frame in ws:
            server.frames.append(frame if isinstance(frame, bytes) else json.loads(frame))
            if frame == json.dumps(deepgram.FINISH_MESSAGE):
                await ws.send(results("Last words.", is_final=True, start=3.0, duration=1.0))
                await ws.send(METADATA)
                await ws.close(1000)

    fake.script = script
    before = our_tasks()
    stream = await provider(fake).open(TranscriptionConfig())
    await stream.send_audio(b"\x00\x01" * 320)
    await stream.finish()
    await stream.finish()  # idempotent
    await stream.send_audio(b"\x00\x01" * 320)  # ignored after finish
    events = await collect(stream)

    assert [(e.text, e.is_final) for e in events] == [("Last words.", True)]
    assert fake.frames == [b"\x00\x01" * 320, {"type": "CloseStream"}]
    assert our_tasks() == before


async def test_finish_times_out_when_server_never_closes(fake: FakeDeepgram):
    stream = await provider(fake, finish_timeout=0.1).open(TranscriptionConfig())
    await stream.finish()
    assert await asyncio.wait_for(collect(stream), 2) == []
    assert not our_tasks()


async def test_close_is_idempotent_and_leaves_no_tasks(fake: FakeDeepgram):
    stream = await provider(fake).open(TranscriptionConfig())
    assert isinstance(stream, DeepgramStream)
    assert len(our_tasks()) == 1
    await stream.close()
    await stream.close()
    await stream.send_audio(b"\x00\x00")  # no-op after close
    assert await collect(stream) == []
    assert not our_tasks()


async def test_server_error_message_raises(fake: FakeDeepgram):
    async def script(server: FakeDeepgram, ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "Error", "variant": "SchemaError", "description": "bad message"}))
        await ws.close(1000)

    fake.script = script
    stream = await provider(fake).open(TranscriptionConfig())
    with pytest.raises(TranscriptionError, match="SchemaError.*bad message"):
        await collect(stream)
    assert not our_tasks()


async def test_server_close_with_error_code_raises(fake: FakeDeepgram):
    async def script(server: FakeDeepgram, ws: ServerConnection) -> None:
        await ws.close(1011, "NET-0001")

    fake.script = script
    stream = await provider(fake).open(TranscriptionConfig())
    await asyncio.sleep(0.1)  # nobody is reading events yet; the writer notices the closure
    with pytest.raises(TranscriptionError, match="no longer connected"):
        await stream.send_audio(b"\x00\x00")
    with pytest.raises(TranscriptionError, match="1011 NET-0001"):
        await collect(stream)
    await stream.send_audio(b"\x00\x00")  # no-op once the stream has been torn down


async def test_abrupt_disconnect_and_unexpected_clean_close_raise(fake: FakeDeepgram):
    async def drop(server: FakeDeepgram, ws: ServerConnection) -> None:
        ws.transport.close()

    fake.script = drop
    stream = await provider(fake).open(TranscriptionConfig())
    with pytest.raises(TranscriptionError, match="dropped"):
        await collect(stream)

    async def clean(server: FakeDeepgram, ws: ServerConnection) -> None:
        await ws.close(1000)

    fake.script = clean
    stream = await provider(fake).open(TranscriptionConfig())
    with pytest.raises(TranscriptionError, match="early"):
        await collect(stream)


async def test_auth_rejection_on_connect(fake: FakeDeepgram):
    fake.reject_status = HTTPStatus.UNAUTHORIZED
    with pytest.raises(TranscriptionError, match=r"rejected the API key \(HTTP 401\): Invalid credentials"):
        await provider(fake).open(TranscriptionConfig())
    assert not our_tasks()


async def test_connection_refused_and_timeout_raise(fake: FakeDeepgram):
    with pytest.raises(TranscriptionError, match="could not connect"):
        await DeepgramProvider("k", url="ws://127.0.0.1:1").open(TranscriptionConfig())

    release = asyncio.Event()

    async def hang(connection: ServerConnection, request):
        await release.wait()

    async with serve(lambda ws: None, "127.0.0.1", 0, process_request=hang) as slow:
        port = slow.sockets[0].getsockname()[1]
        with pytest.raises(TranscriptionError, match="timed out"):
            await DeepgramProvider("k", url=f"ws://127.0.0.1:{port}", connect_timeout=0.1).open(TranscriptionConfig())
        release.set()
