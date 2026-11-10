"""Deepgram streaming speech-to-text (Nova-3 over `wss://api.deepgram.com/v1/listen`).

Event mapping (Deepgram -> `TranscriptEvent`):

* A `Results` message with `is_final: false` is an interim hypothesis for the segment currently being
  transcribed; its transcript is cumulative for that segment, so it is emitted as `is_final=False`.
* A `Results` message with `is_final: true` closes that segment: Deepgram never re-sends its words, so
  each one is emitted exactly once as `is_final=True`. `speech_final` (Deepgram's VAD "the speaker
  paused" flag) is deliberately ignored: a pause does not change which words are final, and long
  utterances produce several `is_final` segments before one `speech_final`.
* `start`/`duration` (seconds) become `start_ms`/`end_ms`; the alternative's `confidence` is passed through.
* Empty transcripts (silence, the flush after `CloseStream`) are skipped. `Metadata`, `UtteranceEnd`
  and `SpeechStarted` messages are ignored. An `Error` message ends the stream with `TranscriptionError`.

Audio: PCM16 little-endian mono (`encoding=linear16`), forwarded as binary frames exactly as received.
Silence: Deepgram closes the socket (1011 `NET-0001`) after ~10 s without a frame, so a text
`{"type": "KeepAlive"}` is sent whenever no audio has been sent for `keepalive_interval` seconds.
Finish: `{"type": "CloseStream"}` makes Deepgram flush, send the last `Results` and `Metadata`, then close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)
from websockets.protocol import State

from app.transcription.base import (
    TranscriptEvent,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionStream,
)

log = logging.getLogger(__name__)

# --- Wire protocol -------------------------------------------------------------------------------
# Verified against developers.deepgram.com (reference/speech-to-text/listen-streaming, docs/keyterm,
# docs/audio-keep-alive, docs/close-stream, docs/stt-troubleshooting-websocket-data-and-net-errors),
# November 2026. Everything Deepgram-specific that could drift lives in this block.
DEFAULT_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"  # keyterm prompting needs Nova-3 (or Flux, which is a different endpoint)
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Token"  # `Authorization: Token <api key>`
ENCODING = "linear16"  # PCM signed 16-bit little-endian
KEYTERM_PARAM = "keyterm"  # repeated once per term; a phrase is one URL-encoded value
KEYTERM_MAX_TOKENS = 500  # Deepgram's budget across all keyterms, counted in model tokens
KEYTERM_MAX_COUNT = 100
KEEPALIVE_MESSAGE = {"type": "KeepAlive"}  # must be a text frame; Deepgram does not answer it
FINISH_MESSAGE = {"type": "CloseStream"}  # server flushes, sends Results + Metadata, closes the socket
MSG_RESULTS = "Results"
MSG_ERROR = "Error"
ERROR_HEADER = "dg-error"  # set on failed upgrades alongside `dg-request-id`
# ------------------------------------------------------------------------------------------------

DEFAULT_KEEPALIVE_INTERVAL = 4.0  # Deepgram recommends every 3-5 s; the idle timeout is 10 s
OUTBOX_MAX_CHUNKS = 256  # ~5-10 s of 20-40 ms frames before the oldest audio is dropped
CLOSE_TIMEOUT = 2.0  # how long `close()` waits for the peer's close frame before aborting the socket


def select_keyterms(keyterms: list[str]) -> list[str]:
    """Trim, de-duplicate (case-insensitively) and cap keyterms to Deepgram's limits, keeping order.

    Deepgram counts model tokens; with no tokenizer available this estimates ~4 characters per token
    and stops adding terms once the estimate reaches the budget, so the request is never rejected.
    """
    chosen: list[str] = []
    seen: set[str] = set()
    tokens = 0
    for raw in keyterms:
        term = " ".join(raw.split())
        key = term.lower()
        if not term or key in seen:
            continue
        cost = max(1, -(-len(term) // 4))
        if tokens + cost > KEYTERM_MAX_TOKENS or len(chosen) >= KEYTERM_MAX_COUNT:
            break
        seen.add(key)
        chosen.append(term)
        tokens += cost
    return chosen


def build_query(config: TranscriptionConfig, *, model: str, endpointing_ms: int | None) -> list[tuple[str, str]]:
    """Query parameters for the listen URL, as (name, value) pairs so `keyterm` can repeat."""
    params = [
        ("model", model),
        ("language", config.language),
        ("encoding", ENCODING),
        ("sample_rate", str(config.sample_rate)),
        ("channels", "1"),
        ("interim_results", "true" if config.interim_results else "false"),
        # Punctuation/casing for readable captions. Not `smart_format`: it holds finals back for up
        # to 3 s of silence while it waits for a number/entity to complete, e.g. "go to slide three".
        ("punctuate", "true"),
    ]
    if endpointing_ms is not None:
        params.append(("endpointing", str(endpointing_ms)))
    params.extend((KEYTERM_PARAM, term) for term in select_keyterms(config.keyterms))
    return params


def parse_results(message: dict) -> TranscriptEvent | None:
    """Map a `Results` message to an event; None when it carries no words."""
    alternatives = (message.get("channel") or {}).get("alternatives") or []
    if not alternatives:
        return None
    best = alternatives[0]
    text = (best.get("transcript") or "").strip()
    if not text:
        return None
    start = message.get("start")
    duration = message.get("duration")
    start_ms = round(start * 1000) if start is not None else None
    end_ms = round((start + duration) * 1000) if start is not None and duration is not None else None
    return TranscriptEvent(
        text=text,
        is_final=bool(message.get("is_final")),
        start_ms=start_ms,
        end_ms=end_ms,
        confidence=best.get("confidence"),
    )


def _describe_close(exc: ConnectionClosed) -> str:
    if exc.rcvd is None:
        return "connection dropped without a close frame"
    return f"connection closed by Deepgram ({exc.rcvd.code} {exc.rcvd.reason or ''})".rstrip()


class DeepgramStream(TranscriptionStream):
    """One live Deepgram connection.

    Audio goes through a bounded outbox drained by one writer task, so `send_audio` never waits on
    the network; when the outbox is full the oldest audio is dropped (live captions prefer fresh audio
    over complete audio). The writer also sends KeepAlive while the outbox is idle. `events()` reads
    the socket directly and owns teardown: when it ends, for any reason, the stream is closed.
    """

    def __init__(self, ws: ClientConnection, *, keepalive_interval: float, finish_timeout: float) -> None:
        self._ws = ws
        self._keepalive_interval = keepalive_interval
        self._finish_timeout = finish_timeout
        self._outbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=OUTBOX_MAX_CHUNKS)
        self._finishing = False
        self._closed = False
        self._dropped = 0
        self._writer = asyncio.create_task(self._write_loop(), name="deepgram-writer")

    async def send_audio(self, chunk: bytes) -> None:
        """Queue PCM16 audio. No-op after `finish()`/`close()`; raises if the connection has failed."""
        if not chunk or self._finishing or self._closed:
            return  # Deepgram treats an empty binary frame as end-of-stream, so never forward one.
        if self._ws.state is not State.OPEN:
            raise TranscriptionError("Deepgram stream is no longer connected")
        self._enqueue(chunk)

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                message = self._decode(raw)
                kind = message.get("type")
                if kind == MSG_RESULTS:
                    event = parse_results(message)
                    if event is not None:
                        yield event
                elif kind == MSG_ERROR:
                    raise TranscriptionError(
                        f"Deepgram error {message.get('variant') or message.get('code') or ''}: "
                        f"{message.get('description') or message}"
                    )
        except ConnectionClosedError as exc:
            if not self._closed:
                raise TranscriptionError(f"Deepgram {_describe_close(exc)}") from exc
        else:
            # Iteration ends quietly on a 1000/1001 close; that is only expected after CloseStream.
            if not (self._finishing or self._closed):
                raise TranscriptionError(
                    f"Deepgram ended the stream early ({self._ws.close_code} {self._ws.close_reason or ''})".rstrip()
                )
        finally:
            await self.close()

    async def finish(self) -> None:
        """Send CloseStream after the queued audio; `events()` ends once Deepgram has flushed."""
        if self._finishing or self._closed:
            return
        self._finishing = True
        self._enqueue(None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._writer
        await self._ws.close()

    def _enqueue(self, item: bytes | None) -> None:
        if self._outbox.full():
            self._outbox.get_nowait()
            self._dropped += 1
            if self._dropped == 1:
                log.warning("deepgram outbox full; dropping oldest audio (network stalled?)")
        self._outbox.put_nowait(item)

    @staticmethod
    def _decode(raw: str) -> dict:
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise TranscriptionError(f"Deepgram sent unparseable JSON: {raw[:200]!r}") from exc
        return message if isinstance(message, dict) else {}

    async def _write_loop(self) -> None:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._outbox.get(), self._keepalive_interval)
                except TimeoutError:
                    await self._ws.send(json.dumps(KEEPALIVE_MESSAGE))
                    continue
                if item is None:
                    await self._ws.send(json.dumps(FINISH_MESSAGE))
                    await self._wait_for_server_close()
                    return
                await self._ws.send(item)
        except ConnectionClosed:
            return  # `events()` reports the closure to the consumer.

    async def _wait_for_server_close(self) -> None:
        try:
            await asyncio.wait_for(self._ws.wait_closed(), self._finish_timeout)
        except TimeoutError:
            log.warning("Deepgram did not close within %.1fs of CloseStream; closing", self._finish_timeout)
            await self._ws.close()


class DeepgramProvider(TranscriptionProvider):
    """Streaming provider for Deepgram. `url` overrides the listen endpoint (tests point it at a fake)."""

    name = "deepgram"

    def __init__(
        self,
        api_key: str,
        *,
        url: str | None = None,
        model: str = DEFAULT_MODEL,
        endpointing_ms: int | None = None,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        connect_timeout: float = 10.0,
        finish_timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._url = url or DEFAULT_URL
        self._model = model
        self._endpointing_ms = endpointing_ms
        self._keepalive_interval = keepalive_interval
        self._connect_timeout = connect_timeout
        self._finish_timeout = finish_timeout

    def listen_url(self, config: TranscriptionConfig) -> str:
        return f"{self._url}?{urlencode(build_query(config, model=self._model, endpointing_ms=self._endpointing_ms))}"

    async def open(self, config: TranscriptionConfig) -> TranscriptionStream:
        if config.encoding != "pcm16":
            raise TranscriptionError(f"Deepgram provider only streams pcm16 audio, not {config.encoding!r}")
        headers = {AUTH_HEADER: f"{AUTH_SCHEME} {self._api_key}"}
        try:
            ws = await connect(
                self.listen_url(config),
                additional_headers=headers,
                open_timeout=self._connect_timeout,
                close_timeout=CLOSE_TIMEOUT,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            detail = exc.response.headers.get(ERROR_HEADER) or exc.response.reason_phrase
            what = "rejected the API key" if status in (401, 403) else "refused the connection"
            raise TranscriptionError(f"Deepgram {what} (HTTP {status}): {detail}") from exc
        except TimeoutError as exc:
            raise TranscriptionError(f"timed out after {self._connect_timeout:.0f}s connecting to Deepgram") from exc
        except (OSError, InvalidURI, InvalidHandshake) as exc:
            raise TranscriptionError(f"could not connect to Deepgram: {exc}") from exc
        return DeepgramStream(ws, keepalive_interval=self._keepalive_interval, finish_timeout=self._finish_timeout)
