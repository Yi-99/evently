"""AssemblyAI streaming provider against a fake `/v3/ws` server on 127.0.0.1 (no network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from websockets.asyncio.server import ServerConnection, serve

from app.transcription import assemblyai
from app.transcription.assemblyai import AssemblyAIProvider, AssemblyAIStream
from app.transcription.base import TranscriptEvent, TranscriptionConfig, TranscriptionError

Script = Callable[["FakeAssemblyAI", ServerConnection], Awaitable[None]]


def begin(model: str = "universal-3-6-pro") -> str:
    return json.dumps({"type": "Begin", "id": "sess-1", "expires_at": 1_900_000_000, "configuration": {"model": model}})


def words(*specs: tuple[str, int, int, float, bool]) -> list[dict]:
    return [{"text": t, "start": s, "end": e, "confidence": c, "word_is_final": f} for t, s, e, c, f in specs]


def turn(order: int, transcript: str, *, end: bool, formatted: bool, word_list: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "type": "Turn",
            "turn_order": order,
            "turn_is_formatted": formatted,
            "end_of_turn": end,
            "transcript": transcript,
            "end_of_turn_confidence": 1.0 if end else 0.0,
            "words": word_list if word_list is not None else [],
            "utterance": transcript if end else "",
        }
    )


TERMINATION = json.dumps({"type": "Termination", "audio_duration_seconds": 1, "session_duration_seconds": 2})


class FakeAssemblyAI:
    """Sends `Begin` on connect, records every frame; `script` drives what else the server does."""

    def __init__(self) -> None:
        self.url = ""
        self.query: dict[str, list[str]] = {}
        self.headers = None  # websockets Headers (case-insensitive)
        self.frames: list[bytes | dict] = []
        self.begin: str | None = begin()
        self.reject_status: HTTPStatus | None = None
        self.script: Script | None = None

    async def process_request(self, connection: ServerConnection, request):
        if self.reject_status is not None:
            return connection.respond(self.reject_status, "Unauthorized\n")
        return None

    async def handler(self, connection: ServerConnection) -> None:
        self.query = parse_qs(urlsplit(connection.request.path).query)
        self.headers = connection.request.headers
        if self.begin is not None:
            await connection.send(self.begin)
        if self.script is not None:
            await self.script(self, connection)
            return
        await self.record(connection)

    async def record(self, connection: ServerConnection) -> None:
        async for frame in connection:
            self.frames.append(frame if isinstance(frame, bytes) else json.loads(frame))

    def texts(self) -> list[dict]:
        return [f for f in self.frames if isinstance(f, dict)]

    def audio(self) -> list[bytes]:
        return [f for f in self.frames if isinstance(f, bytes)]


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[FakeAssemblyAI]:
    server = FakeAssemblyAI()
    async with serve(server.handler, "127.0.0.1", 0, process_request=server.process_request) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        server.url = f"ws://127.0.0.1:{port}/v3/ws"
        yield server


def provider(fake: FakeAssemblyAI, **kwargs) -> AssemblyAIProvider:
    kwargs.setdefault("finish_timeout", 0.5)
    kwargs.setdefault("connect_timeout", 2.0)
    return AssemblyAIProvider("aai-secret", url=fake.url, **kwargs)


async def collect(stream) -> list[TranscriptEvent]:
    return [event async for event in stream.events()]


def our_tasks() -> set[asyncio.Task]:
    return {t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("assemblyai-")}


async def script_then_terminate(frames: list[str]) -> Script:
    async def script(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        for frame in frames:
            await ws.send(frame)
        await ws.send(TERMINATION)
        await ws.close(1000)

    return script


async def test_connect_builds_url_and_auth_header(fake: FakeAssemblyAI):
    config = TranscriptionConfig(
        sample_rate=24000,
        language="en-US",
        interim_results=True,
        keyterms=["next slide", "Evently", " next   slide", "", "x" * 51, "Q3 roadmap"],
    )
    stream = await provider(fake, mode="min_latency").open(config)
    await stream.close()

    assert fake.headers["Authorization"] == "aai-secret"
    assert fake.query["speech_model"] == ["universal-3-6-pro"]
    assert fake.query["encoding"] == ["pcm_s16le"]
    assert fake.query["sample_rate"] == ["24000"]
    assert fake.query["include_partial_turns"] == ["true"]
    assert fake.query["mode"] == ["min_latency"]
    assert json.loads(fake.query["language_codes"][0]) == ["en"]
    assert json.loads(fake.query["keyterms_prompt"][0]) == ["next slide", "Evently", "Q3 roadmap"]
    assert "format_turns" not in fake.query


async def test_universal_streaming_params(fake: FakeAssemblyAI):
    fake.begin = begin("universal-streaming-english")
    stream = await provider(fake, model="universal-streaming-english").open(TranscriptionConfig(interim_results=False))
    await stream.close()
    assert fake.query["speech_model"] == ["universal-streaming-english"]
    assert fake.query["format_turns"] == ["true"]
    assert fake.query["include_partial_turns"] == ["false"]
    assert "language_codes" not in fake.query and "mode" not in fake.query and "keyterms_prompt" not in fake.query


async def test_multilingual_omits_language_steering(fake: FakeAssemblyAI):
    stream = await provider(fake).open(TranscriptionConfig(language="multi"))
    await stream.close()
    assert "language_codes" not in fake.query


def test_select_keyterms_limits():
    assert assemblyai.select_keyterms(["a", "A", " b  c ", "x" * 51]) == ["a", "b c"]
    assert len(assemblyai.select_keyterms([f"t{i}" for i in range(150)])) == 100
    assert assemblyai.language_code("pt_BR") == "pt" and assemblyai.language_code("") is None


async def test_rejects_non_pcm16_config(fake: FakeAssemblyAI):
    with pytest.raises(TranscriptionError, match="pcm16"):
        await provider(fake).open(TranscriptionConfig(encoding="mulaw"))


async def test_pro_turns_map_to_interim_and_single_final(fake: FakeAssemblyAI):
    w_partial = words(("next", 100, 400, 0.9, False), ("slide", 450, 800, 0.8, False))
    w_final = words(("Next", 100, 400, 0.95, True), ("slide.", 450, 820, 0.85, True))
    fake.script = await script_then_terminate(
        [
            json.dumps({"type": "SpeechStarted", "timestamp": 100, "confidence": 0.9}),
            turn(0, "next", end=False, formatted=False, word_list=w_partial[:1]),
            turn(0, "next slide", end=False, formatted=False, word_list=w_partial),
            turn(0, "Next slide.", end=True, formatted=True, word_list=w_final),
            turn(0, "Next slide.", end=True, formatted=True, word_list=w_final),  # duplicate: dropped
            turn(1, "", end=False, formatted=False),  # noise: skipped
            turn(1, "thank you", end=False, formatted=False),
            turn(1, "Thank you.", end=True, formatted=True),
            turn(2, "", end=True, formatted=True),  # closing empty turn: skipped
        ]
    )
    stream = await provider(fake).open(TranscriptionConfig())
    await stream.finish()
    events = await collect(stream)

    assert [(e.text, e.is_final) for e in events] == [
        ("next", False),
        ("next slide", False),
        ("Next slide.", True),
        ("thank you", False),
        ("Thank you.", True),
    ]
    final = events[2]
    assert (final.start_ms, final.end_ms) == (100, 820)
    assert final.confidence == pytest.approx(0.9)
    assert events[4].start_ms is None and events[4].confidence is None


async def test_streaming_model_interim_includes_unfinalised_word(fake: FakeAssemblyAI):
    fake.begin = begin("universal-streaming-english")
    w = words(("hi", 0, 200, 0.9, True), ("my", 210, 400, 0.9, True), ("na", 410, 600, 0.5, False))
    fake.script = await script_then_terminate([turn(0, "hi my", end=False, formatted=False, word_list=w)])
    stream = await provider(fake, model="universal-streaming-english").open(TranscriptionConfig())
    await stream.finish()
    events = await collect(stream)
    assert [(e.text, e.is_final) for e in events] == [("hi my na", False)]


async def test_streaming_model_formatted_final_is_the_final(fake: FakeAssemblyAI):
    fake.begin = begin("universal-streaming-english")
    fake.script = await script_then_terminate(
        [
            turn(0, "my name is", end=False, formatted=False),
            turn(0, "my name is sonny", end=True, formatted=False),
            turn(0, "My name is Sonny.", end=True, formatted=True),
            turn(1, "see you", end=True, formatted=False),  # formatted copy never arrives
            turn(2, "bye", end=False, formatted=False),  # next turn releases the held final
            turn(2, "bye now", end=True, formatted=False),  # held until Termination, then released
        ]
    )
    stream = await provider(fake, model="universal-streaming-english").open(TranscriptionConfig())
    await stream.finish()
    events = await collect(stream)
    assert [(e.text, e.is_final) for e in events] == [
        ("my name is", False),
        ("my name is sonny", False),
        ("My name is Sonny.", True),
        ("see you", False),
        ("see you", True),
        ("bye", False),
        ("bye now", False),
        ("bye now", True),
    ]


async def test_streaming_model_without_format_turns_finalises_immediately(fake: FakeAssemblyAI):
    fake.begin = begin("universal-streaming-english")
    fake.script = await script_then_terminate([turn(0, "my name is sonny", end=True, formatted=False)])
    stream = await provider(fake, model="universal-streaming-english", format_turns=False).open(TranscriptionConfig())
    assert "format_turns" not in fake.query
    await stream.finish()
    events = await collect(stream)
    assert [(e.text, e.is_final) for e in events] == [("my name is sonny", True)]


async def test_audio_is_reframed_into_legal_chunks_and_finish_terminates(fake: FakeAssemblyAI):
    async def script(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        async for frame in ws:
            server.frames.append(frame if isinstance(frame, bytes) else json.loads(frame))
            if frame == json.dumps(assemblyai.TERMINATE_MESSAGE):
                await ws.send(turn(0, "Done.", end=True, formatted=True))
                await ws.send(TERMINATION)
                await ws.close(1000)

    fake.script = script
    before = our_tasks()
    stream = await provider(fake).open(TranscriptionConfig(sample_rate=16000))
    ms = 32  # bytes per ms at 16 kHz PCM16
    for _ in range(3):
        await stream.send_audio(b"\x01\x02" * (10 * ms // 2))  # 10 ms chunks: illegal on their own
    await stream.send_audio(b"\x03\x04" * (30 * ms // 2))  # buffer now 60 ms: flushed as one frame
    await stream.send_audio(b"\x05\x06" * (2500 * ms // 2))  # 2.5 s burst: split into <=1000 ms frames
    await stream.send_audio(b"\x07\x08" * (7 * ms // 2))  # 7 ms tail: padded with silence on finish
    await stream.finish()
    await stream.finish()
    events = await collect(stream)

    assert [(e.text, e.is_final) for e in events] == [("Done.", True)]
    sizes_ms = [len(f) / ms for f in fake.audio()]
    assert sizes_ms == [60, 1000, 1000, 500, 50]
    assert all(assemblyai.MIN_CHUNK_MS <= s <= assemblyai.MAX_CHUNK_MS for s in sizes_ms)
    assert b"".join(fake.audio()).startswith(b"\x01\x02" * 480 + b"\x03\x04")
    assert fake.audio()[-1] == b"\x07\x08" * (7 * ms // 2) + bytes(43 * ms)
    assert fake.texts() == [{"type": "Terminate"}]
    assert our_tasks() == before


async def test_no_keepalive_is_sent_during_silence(fake: FakeAssemblyAI):
    stream = await provider(fake).open(TranscriptionConfig())
    await asyncio.sleep(0.2)
    await stream.close()
    assert fake.frames == []


async def test_finish_times_out_when_server_never_terminates(fake: FakeAssemblyAI):
    stream = await provider(fake, finish_timeout=0.1).open(TranscriptionConfig())
    await stream.finish()
    assert await asyncio.wait_for(collect(stream), 2) == []
    assert not our_tasks()


async def test_close_is_idempotent_and_leaves_no_tasks(fake: FakeAssemblyAI):
    stream = await provider(fake).open(TranscriptionConfig())
    assert isinstance(stream, AssemblyAIStream)
    assert len(our_tasks()) == 1
    await stream.close()
    await stream.close()
    await stream.send_audio(b"\x00\x00")  # no-op after close
    assert await collect(stream) == []
    assert not our_tasks()


async def test_server_error_message_raises(fake: FakeAssemblyAI):
    async def script(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "Error", "error_code": 3007, "error": "Input duration violation"}))
        await ws.close(3007, "See Error message for details")

    fake.script = script
    stream = await provider(fake).open(TranscriptionConfig())
    with pytest.raises(TranscriptionError, match="3007: Input duration violation"):
        await collect(stream)
    assert not our_tasks()


async def test_abrupt_disconnect_and_unexpected_close_raise(fake: FakeAssemblyAI):
    async def drop(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        ws.transport.close()

    fake.script = drop
    stream = await provider(fake).open(TranscriptionConfig())
    await asyncio.sleep(0.1)
    with pytest.raises(TranscriptionError, match="no longer connected"):
        await stream.send_audio(b"\x00\x00")
    with pytest.raises(TranscriptionError, match="dropped"):
        await collect(stream)

    async def expired(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        await ws.close(3008, "Session Expired")

    fake.script = expired
    stream = await provider(fake).open(TranscriptionConfig())
    with pytest.raises(TranscriptionError, match="3008 Session Expired"):
        await collect(stream)

    async def clean(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        await ws.close(1000)

    fake.script = clean
    stream = await provider(fake).open(TranscriptionConfig())
    with pytest.raises(TranscriptionError, match="early"):
        await collect(stream)


async def test_auth_rejection_after_handshake(fake: FakeAssemblyAI):
    fake.begin = None

    async def unauthorized(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        error = {"type": "Error", "error_code": 1008, "error": "Unauthorized Connection: Invalid API key"}
        await ws.send(json.dumps(error))
        await ws.close(1008, "See Error message for details")

    fake.script = unauthorized
    with pytest.raises(TranscriptionError, match="1008: Unauthorized Connection: Invalid API key"):
        await provider(fake).open(TranscriptionConfig())
    assert not our_tasks()


async def test_auth_rejection_at_handshake_and_missing_begin(fake: FakeAssemblyAI):
    fake.reject_status = HTTPStatus.UNAUTHORIZED
    with pytest.raises(TranscriptionError, match=r"rejected the API key \(HTTP 401\)"):
        await provider(fake).open(TranscriptionConfig())

    fake.reject_status = None
    fake.begin = None

    async def silent(server: FakeAssemblyAI, ws: ServerConnection) -> None:
        await server.record(ws)

    fake.script = silent
    with pytest.raises(TranscriptionError, match="Begin"):
        await provider(fake, connect_timeout=0.1).open(TranscriptionConfig())


async def test_connection_refused_raises():
    with pytest.raises(TranscriptionError, match="could not connect"):
        await AssemblyAIProvider("k", url="ws://127.0.0.1:1").open(TranscriptionConfig())
