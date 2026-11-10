"""AssemblyAI streaming speech-to-text (v3 WebSocket, `wss://streaming.assemblyai.com/v3/ws`).

AssemblyAI delivers transcription in *turns* (one continuous unit of speech) and re-transcribes the
whole turn on every `Turn` message, so each message supersedes the previous one for that `turn_order`.

Event mapping (AssemblyAI -> `TranscriptEvent`):

* `Turn` with `end_of_turn: false` is the current hypothesis of the in-progress turn -> `is_final=False`
  with the cumulative text. On the Universal Streaming models `transcript` holds only the words that
  are already immutable while `words` also carries the trailing in-progress word, so the interim text is
  the longer of the two when they agree on the prefix; on Universal-3.x Pro they are the same text.
* `Turn` with `end_of_turn: true` closes the turn -> one `is_final=True` event. `turn_order` is
  monotonic and every message for a turn arrives before the next turn, so anything for an already
  finalised order is dropped: a turn is never emitted twice.
* Formatted vs unformatted finals: Universal-3.x Pro always sends one formatted final. The Universal
  Streaming models send an unformatted final (`turn_is_formatted: false`) and, only with
  `format_turns=true`, a formatted final for the same order right after. When formatting was requested
  the unformatted final is surfaced as an interim (its text is already complete, so a keyword trigger
  does not wait on formatting) and the formatted one is the final; if the formatted copy never comes,
  the held unformatted text is emitted as the final when the next turn starts or the session ends.
* `start_ms`/`end_ms` come from the first/last word, `confidence` is the mean word confidence.
* Empty transcripts (the "closing Turn" after Terminate, noise) are skipped. `Begin`, `SpeechStarted`,
  `Heartbeat`, `SpeakerRevision` are ignored; `Error` ends the stream with `TranscriptionError`;
  `Termination` ends it cleanly.

Audio: PCM16 LE mono (`encoding=pcm_s16le`). Every binary frame must hold 20-1000 ms of audio or the
session is closed with error 3007, so incoming chunks are re-framed into ~50 ms frames (the vendor's
recommended size) regardless of how the caller chunks them.
Silence: no keep-alive is needed. Sessions stay open until `Terminate` or the 3-hour maximum (close
code 3008); `KeepAlive` only matters with the optional `inactivity_timeout`, which this provider does
not set. The library's WebSocket pings still detect a dead peer.
Finish: `{"type": "Terminate"}` after the queued audio; the server flushes the open turn, sends
`Termination`, then closes with 1000. Closing before `Termination` discards the last transcript.
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
# Verified against assemblyai.com/docs (streaming/api-spec/streaming-websocket, streaming/message-sequence,
# streaming/prompting-and-keyterms, streaming/multilingual-transcription, streaming/select-the-speech-model,
# streaming/common-session-errors-and-closures), November 2026. Everything vendor-specific lives here.
DEFAULT_URL = "wss://streaming.assemblyai.com/v3/ws"  # also streaming.{us,eu}.assemblyai.com for residency
DEFAULT_MODEL = "universal-3-6-pro"  # vendor default; alternatives: universal-3-5-pro, universal-streaming-*
PRO_MODEL_PREFIX = "universal-3-"  # models that take `language_codes`/`mode`; always-formatted finals
STREAMING_MODEL_PREFIX = "universal-streaming"  # models that take `format_turns`; no language parameter
AUTH_HEADER = "Authorization"  # bare API key, no `Bearer`/`Token` scheme
ENCODING = "pcm_s16le"
KEYTERMS_PARAM = "keyterms_prompt"  # JSON-encoded array in the query string
KEYTERMS_MAX_COUNT = 100  # more -> request error
KEYTERM_MAX_CHARS = 50  # longer terms are silently ignored by the server
LANGUAGE_PARAM = "language_codes"  # JSON-encoded array of ISO 639-1 codes; omit for native code-switching
MIN_CHUNK_MS = 20  # frames outside 20-1000 ms close the session with error 3007
MAX_CHUNK_MS = 1000
SEND_CHUNK_MS = 50  # vendor recommends 50-100 ms frames
TERMINATE_MESSAGE = {"type": "Terminate"}
MSG_BEGIN = "Begin"
MSG_TURN = "Turn"
MSG_TERMINATION = "Termination"
MSG_ERROR = "Error"
# ------------------------------------------------------------------------------------------------

OUTBOX_MAX_CHUNKS = 256  # ~5-10 s of 20-40 ms frames before the oldest audio is dropped
CLOSE_TIMEOUT = 2.0  # how long `close()` waits for the peer's close frame before aborting the socket


def select_keyterms(keyterms: list[str]) -> list[str]:
    """Trim, drop over-long terms (the server ignores them anyway), de-duplicate and cap to 100."""
    chosen: list[str] = []
    seen: set[str] = set()
    for raw in keyterms:
        term = " ".join(raw.split())
        key = term.lower()
        if not term or len(term) > KEYTERM_MAX_CHARS or key in seen:
            continue
        seen.add(key)
        chosen.append(term)
        if len(chosen) == KEYTERMS_MAX_COUNT:
            break
    return chosen


def language_code(language: str) -> str | None:
    """ISO 639-1 primary subtag of a BCP-47 tag ("en-US" -> "en"); None when no steering is wanted."""
    primary = language.replace("_", "-").split("-")[0].strip().lower()
    if primary in ("", "multi", "auto"):
        return None
    return primary


def build_query(
    config: TranscriptionConfig, *, model: str, mode: str | None, format_turns: bool
) -> list[tuple[str, str]]:
    """Connection parameters. Unknown parameters are silently ignored by the server, hence the model gating."""
    params = [
        ("speech_model", model),
        ("encoding", ENCODING),
        ("sample_rate", str(config.sample_rate)),
        ("include_partial_turns", "true" if config.interim_results else "false"),
    ]
    if model.startswith(PRO_MODEL_PREFIX):
        code = language_code(config.language)
        if code:
            params.append((LANGUAGE_PARAM, json.dumps([code])))
        if mode:
            params.append(("mode", mode))
    elif model.startswith(STREAMING_MODEL_PREFIX) and format_turns:
        params.append(("format_turns", "true"))
    terms = select_keyterms(config.keyterms)
    if terms:
        params.append((KEYTERMS_PARAM, json.dumps(terms)))
    return params


def interim_text(message: dict) -> str:
    """Cumulative text of an in-progress turn, including a trailing word the server has not finalised."""
    transcript = (message.get("transcript") or "").strip()
    joined = " ".join(w.get("text", "") for w in message.get("words") or [] if w.get("text")).strip()
    if len(joined) > len(transcript) and joined.startswith(transcript):
        return joined
    return transcript


def turn_event(message: dict, *, text: str, is_final: bool) -> TranscriptEvent:
    words = [w for w in message.get("words") or [] if isinstance(w, dict)]
    confidences = [w["confidence"] for w in words if isinstance(w.get("confidence"), int | float)]
    return TranscriptEvent(
        text=text,
        is_final=is_final,
        start_ms=words[0].get("start") if words else None,
        end_ms=words[-1].get("end") if words else None,
        confidence=sum(confidences) / len(confidences) if confidences else None,
    )


def _describe_close(exc: ConnectionClosed) -> str:
    if exc.rcvd is None:
        return "connection dropped without a close frame"
    return f"connection closed by AssemblyAI ({exc.rcvd.code} {exc.rcvd.reason or ''})".rstrip()


def _error_text(message: dict) -> str:
    code = message.get("error_code")
    text = message.get("error") or json.dumps(message)
    return f"AssemblyAI error {code}: {text}" if code is not None else f"AssemblyAI error: {text}"


class AssemblyAIStream(TranscriptionStream):
    """One live AssemblyAI session (already past `Begin`).

    Audio goes through a bounded outbox drained by one writer task that re-frames it into legal chunk
    sizes, so `send_audio` never waits on the network; when the outbox is full the oldest audio is
    dropped. `events()` reads the socket directly and owns teardown: when it ends, the stream is closed.
    """

    def __init__(
        self, ws: ClientConnection, *, sample_rate: int, expect_formatted_finals: bool, finish_timeout: float
    ) -> None:
        self._ws = ws
        self._expect_formatted_finals = expect_formatted_finals
        self._finish_timeout = finish_timeout
        bytes_per_ms = sample_rate * 2 // 1000
        self._send_bytes = bytes_per_ms * SEND_CHUNK_MS
        self._max_bytes = bytes_per_ms * MAX_CHUNK_MS
        self._outbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=OUTBOX_MAX_CHUNKS)
        self._finishing = False
        self._closed = False
        self._dropped = 0
        self._last_final_order = -1
        self._held_final: tuple[int, TranscriptEvent] | None = None  # unformatted final awaiting its formatted copy
        self._writer = asyncio.create_task(self._write_loop(), name="assemblyai-writer")

    async def send_audio(self, chunk: bytes) -> None:
        """Queue PCM16 audio. No-op after `finish()`/`close()`; raises if the connection has failed."""
        if not chunk or self._finishing or self._closed:
            return
        if self._ws.state is not State.OPEN:
            raise TranscriptionError("AssemblyAI stream is no longer connected")
        self._enqueue(chunk)

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                message = self._decode(raw)
                kind = message.get("type")
                if kind == MSG_TURN:
                    for event in self._map_turn(message):
                        yield event
                elif kind == MSG_TERMINATION:
                    break
                elif kind == MSG_ERROR:
                    raise TranscriptionError(_error_text(message))
            else:
                # Iteration ends quietly on a 1000/1001 close; that is only expected after Terminate.
                if not (self._finishing or self._closed):
                    reason = f"{self._ws.close_code} {self._ws.close_reason or ''}".rstrip()
                    raise TranscriptionError(f"AssemblyAI ended the session early ({reason})")
        except ConnectionClosedError as exc:
            if not self._closed:
                raise TranscriptionError(f"AssemblyAI {_describe_close(exc)}") from exc
        finally:
            await self.close()
        held = self._release_held_final()
        if held is not None:
            yield held

    async def finish(self) -> None:
        """Send Terminate after the queued audio; `events()` ends on the server's `Termination`."""
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

    def _map_turn(self, message: dict) -> list[TranscriptEvent]:
        order = message.get("turn_order", 0)
        if order <= self._last_final_order:
            return []
        events: list[TranscriptEvent] = []
        if self._held_final is not None and order > self._held_final[0]:
            held = self._release_held_final()
            if held is not None:
                events.append(held)
        if not message.get("end_of_turn"):
            text = interim_text(message)
            if text:
                events.append(turn_event(message, text=text, is_final=False))
            return events
        text = (message.get("transcript") or "").strip()
        if self._expect_formatted_finals and not message.get("turn_is_formatted"):
            if text:
                self._held_final = (order, turn_event(message, text=text, is_final=True))
                events.append(turn_event(message, text=text, is_final=False))
            return events
        self._held_final = None
        self._last_final_order = order
        if text:
            events.append(turn_event(message, text=text, is_final=True))
        return events

    def _release_held_final(self) -> TranscriptEvent | None:
        if self._held_final is None:
            return None
        order, event = self._held_final
        self._held_final = None
        self._last_final_order = max(self._last_final_order, order)
        return event

    def _enqueue(self, item: bytes | None) -> None:
        if self._outbox.full():
            self._outbox.get_nowait()
            self._dropped += 1
            if self._dropped == 1:
                log.warning("assemblyai outbox full; dropping oldest audio (network stalled?)")
        self._outbox.put_nowait(item)

    @staticmethod
    def _decode(raw: str) -> dict:
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise TranscriptionError(f"AssemblyAI sent unparseable JSON: {raw[:200]!r}") from exc
        return message if isinstance(message, dict) else {}

    def _take_frames(self, buffer: bytearray) -> list[bytes]:
        """Cut whole frames of SEND_CHUNK_MS..MAX_CHUNK_MS (on sample boundaries) off the front of `buffer`."""
        frames: list[bytes] = []
        while len(buffer) >= self._send_bytes:
            size = min(len(buffer), self._max_bytes) & ~1
            frames.append(bytes(buffer[:size]))
            del buffer[:size]
        return frames

    async def _write_loop(self) -> None:
        buffer = bytearray()
        try:
            while True:
                item = await self._outbox.get()
                if item is None:
                    if buffer:
                        # Pad the tail with silence so the last frame is long enough to be legal.
                        buffer.extend(bytes(max(0, self._send_bytes - len(buffer))))
                        await self._ws.send(bytes(buffer))
                    await self._ws.send(json.dumps(TERMINATE_MESSAGE))
                    await self._wait_for_server_close()
                    return
                buffer.extend(item)
                for frame in self._take_frames(buffer):
                    await self._ws.send(frame)
        except ConnectionClosed:
            return  # `events()` reports the closure to the consumer.

    async def _wait_for_server_close(self) -> None:
        try:
            await asyncio.wait_for(self._ws.wait_closed(), self._finish_timeout)
        except TimeoutError:
            log.warning("AssemblyAI did not close within %.1fs of Terminate; closing", self._finish_timeout)
            await self._ws.close()


class AssemblyAIProvider(TranscriptionProvider):
    """Streaming provider for AssemblyAI. `url` overrides the endpoint (regional host, or a fake in tests).

    `mode` (`min_latency` | `balanced` | `max_accuracy`) applies to Universal-3.x Pro; None keeps the
    server default (`balanced`). `format_turns` applies to the Universal Streaming models only.
    """

    name = "assemblyai"

    def __init__(
        self,
        api_key: str,
        *,
        url: str | None = None,
        model: str = DEFAULT_MODEL,
        mode: str | None = None,
        format_turns: bool = True,
        connect_timeout: float = 10.0,
        finish_timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._url = url or DEFAULT_URL
        self._model = model
        self._mode = mode
        self._format_turns = format_turns
        self._connect_timeout = connect_timeout
        self._finish_timeout = finish_timeout

    def session_url(self, config: TranscriptionConfig) -> str:
        query = build_query(config, model=self._model, mode=self._mode, format_turns=self._format_turns)
        return f"{self._url}?{urlencode(query)}"

    async def open(self, config: TranscriptionConfig) -> TranscriptionStream:
        if config.encoding != "pcm16":
            raise TranscriptionError(f"AssemblyAI provider only streams pcm16 audio, not {config.encoding!r}")
        try:
            ws = await connect(
                self.session_url(config),
                additional_headers={AUTH_HEADER: self._api_key},
                open_timeout=self._connect_timeout,
                close_timeout=CLOSE_TIMEOUT,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            what = "rejected the API key" if status in (401, 403) else "refused the connection"
            raise TranscriptionError(f"AssemblyAI {what} (HTTP {status}): {exc.response.reason_phrase}") from exc
        except TimeoutError as exc:
            raise TranscriptionError(f"timed out after {self._connect_timeout:.0f}s connecting to AssemblyAI") from exc
        except (OSError, InvalidURI, InvalidHandshake) as exc:
            raise TranscriptionError(f"could not connect to AssemblyAI: {exc}") from exc
        try:
            await self._await_begin(ws)
        except BaseException:
            await ws.close()
            raise
        expect_formatted = self._model.startswith(STREAMING_MODEL_PREFIX) and self._format_turns
        return AssemblyAIStream(
            ws,
            sample_rate=config.sample_rate,
            expect_formatted_finals=expect_formatted,
            finish_timeout=self._finish_timeout,
        )

    async def _await_begin(self, ws: ClientConnection) -> None:
        """Wait for `Begin`; credential/quota problems arrive as an `Error` then a 1008 close instead."""
        try:
            async with asyncio.timeout(self._connect_timeout):
                while True:
                    raw = await ws.recv()
                    message = json.loads(raw) if isinstance(raw, str) else {}
                    kind = message.get("type") if isinstance(message, dict) else None
                    if kind == MSG_BEGIN:
                        applied = (message.get("configuration") or {}).get("model")
                        if applied and applied != self._model:
                            log.warning("AssemblyAI applied model %s instead of %s", applied, self._model)
                        return
                    if kind == MSG_ERROR:
                        raise TranscriptionError(_error_text(message))
        except TimeoutError as exc:
            raise TranscriptionError("AssemblyAI did not send Begin in time") from exc
        except ConnectionClosed as exc:
            raise TranscriptionError(f"AssemblyAI {_describe_close(exc)} before the session began") from exc
        except ValueError as exc:
            raise TranscriptionError(f"AssemblyAI sent unparseable JSON before Begin: {exc}") from exc
