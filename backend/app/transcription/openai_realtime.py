"""OpenAI Realtime transcription provider.

Streams the speaker's PCM16 audio to OpenAI's realtime transcription WebSocket and maps
its events onto `TranscriptEvent`s:

* `conversation.item.input_audio_transcription.delta`     -> cumulative interim events
* `conversation.item.input_audio_transcription.completed` -> exactly one final event per turn

The recommended live model (`gpt-live-transcribe`) has no server-side turn detection, so
this module also carries a small client-side endpointer that decides when to commit a turn,
and a pure-Python resampler because the API only accepts 24 kHz audio while callers usually
send 16 kHz.

Everything that touches the wire format lives in the "WIRE PROTOCOL" section below.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import sys
from array import array
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from app.transcription.base import (
    TranscriptEvent,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionStream,
)

log = logging.getLogger(__name__)

# =====================================================================================
# WIRE PROTOCOL — verified against OpenAI's docs on 2026-11-09:
#   https://developers.openai.com/api/docs/guides/realtime-transcription
#   https://developers.openai.com/api/reference/resources/realtime  (connect params, events)
#   https://developers.openai.com/api/reference/resources/realtime/subresources/client_secrets
#   https://developers.openai.com/api/docs/guides/realtime-vad
# =====================================================================================

#: Transcription sessions use `intent=transcription` and MUST NOT carry `model=` in the URL.
DEFAULT_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
#: Recommended streaming model; `gpt-transcribe` uses the same commit-driven flow.
DEFAULT_MODEL = "gpt-live-transcribe"
#: The only PCM sample rate the API accepts ("audio/pcm", 24 kHz, 16-bit mono LE).
API_SAMPLE_RATE = 24000

#: Models that take `languages: [...]` and `keywords: [...]`; older models take `language`
#: and a free-text `prompt` instead.
_LIST_HINT_MODELS = frozenset({"gpt-live-transcribe", "gpt-transcribe"})
#: `prompt` is rejected for this model.
_NO_PROMPT_MODELS = frozenset({"gpt-realtime-whisper"})

# Client events.
_EV_SESSION_UPDATE = "session.update"
_EV_APPEND = "input_audio_buffer.append"
_EV_COMMIT = "input_audio_buffer.commit"
# Server events.
_EV_SESSION_UPDATED = ("session.updated", "transcription_session.updated")
_EV_COMMITTED = "input_audio_buffer.committed"
_EV_DELTA = "conversation.item.input_audio_transcription.delta"
_EV_COMPLETED = "conversation.item.input_audio_transcription.completed"
_EV_FAILED = "conversation.item.input_audio_transcription.failed"
_EV_ERROR = "error"

#: Committing an empty buffer is an error; the API historically required >= 100 ms.
_MIN_COMMIT_MS = 100.0


def _session_update(
    config: TranscriptionConfig,
    *,
    model: str,
    delay: str | None,
    prompt: str | None,
    noise_reduction: str | None,
) -> dict[str, Any]:
    """Build the `session.update` that configures a transcription session.

    Turn detection is always off: `gpt-live-transcribe` does not support server VAD, so the
    client commits each turn (see `_Endpointer`). `config.keyterms` become `keywords` (or are
    folded into `prompt` for models without keyword support); `config.language` becomes
    `languages` / `language`.
    """
    transcription: dict[str, Any] = {"model": model}
    languages = _language_codes(config.language)
    keywords = _clean_keywords(config.keyterms)
    if model in _LIST_HINT_MODELS:
        if languages:
            transcription["languages"] = languages
        if keywords:
            transcription["keywords"] = keywords
        if prompt:
            transcription["prompt"] = prompt
    else:
        if languages:
            transcription["language"] = languages[0]
        hint = ", ".join(part for part in (prompt, ", ".join(keywords)) if part)
        if hint and model not in _NO_PROMPT_MODELS:
            transcription["prompt"] = hint
    if delay:
        transcription["delay"] = delay

    audio_input: dict[str, Any] = {
        "format": {"type": "audio/pcm", "rate": API_SAMPLE_RATE},
        "transcription": transcription,
        "turn_detection": None,
    }
    if noise_reduction:
        audio_input["noise_reduction"] = {"type": noise_reduction}
    return {"type": _EV_SESSION_UPDATE, "session": {"type": "transcription", "audio": {"input": audio_input}}}


def _append_event(pcm24: bytes) -> str:
    """`input_audio_buffer.append` carrying base64 PCM16 at `API_SAMPLE_RATE`."""
    return json.dumps({"type": _EV_APPEND, "audio": base64.b64encode(pcm24).decode("ascii")})


_COMMIT_EVENT = json.dumps({"type": _EV_COMMIT})


def _language_codes(language: str | None) -> list[str]:
    """Normalise a language setting to the codes the API accepts.

    "en-US" / "en_us" -> ["en"]; regional Chinese codes are kept ("zh-TW" -> ["zh-tw"]);
    comma-separated values give several codes; empty, "auto" or "multi" -> [] (let the model
    decide). The API rejects malformed codes, so this is deliberately conservative.
    """
    codes: list[str] = []
    for raw in (language or "").split(","):
        tag = raw.strip().lower().replace("_", "-")
        if not tag or tag in {"auto", "multi"}:
            continue
        code = tag if tag.startswith("zh-") else tag.split("-", 1)[0]
        if code not in codes:
            codes.append(code)
    return codes


def _clean_keywords(keyterms: list[str]) -> list[str]:
    """Drop blanks, duplicates and terms with characters the API rejects (`<`, `>`, CR, LF)."""
    cleaned: list[str] = []
    for term in keyterms:
        text = " ".join(term.split())
        if text and "<" not in text and ">" not in text and text not in cleaned:
            cleaned.append(text)
    return cleaned


# =====================================================================================
# Audio helpers (pure Python; Python 3.13 has no `audioop`)
# =====================================================================================


def _to_samples(pcm: bytes) -> array:
    """PCM16LE bytes -> array of signed 16-bit samples (raises on a half sample)."""
    if len(pcm) % 2:
        raise ValueError("PCM16 data must contain whole samples (even byte length)")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _to_bytes(samples: array) -> bytes:
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


class Pcm16Resampler:
    """Streaming PCM16 mono resampler using linear interpolation.

    `process()` takes whole little-endian samples (even byte length) and returns the
    resampled bytes; timing is continuous across calls, so feeding chunks gives the same
    output as feeding the whole signal at once (up to the trailing sample `flush()` emits).
    Upsampling (the usual 16 kHz -> 24 kHz) is plain interpolation; when downsampling a
    short moving average runs first to limit aliasing. Not a brick-wall filter — good
    enough for speech recognition, not for music.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._taps = max(1, round(src_rate / dst_rate))
        self._history = array("h")  # last taps-1 raw samples, for the moving average
        self._carry = array("h")  # last filtered sample of the previous call
        # Position of the next output sample relative to `_carry[0]`, in source samples
        # scaled by `dst_rate` so the arithmetic stays in integers (no drift).
        self._pos = 0

    def process(self, pcm: bytes) -> bytes:
        if self.src_rate == self.dst_rate:
            return pcm
        samples = _to_samples(pcm)
        if self._taps > 1:
            samples = self._smooth(samples)
        src = self._carry + samples
        n = len(src)
        out = array("h")
        pos, step, den = self._pos, self.src_rate, self.dst_rate
        limit = (n - 1) * den  # outputs need src[idx + 1]; the rest waits for the next call
        while pos < limit:
            idx, frac = divmod(pos, den)
            a = src[idx]
            out.append(a + ((src[idx + 1] - a) * frac + den // 2) // den)
            pos += step
        if n:
            self._carry = src[-1:]
            self._pos = pos - (n - 1) * den
        return _to_bytes(out)

    def flush(self) -> bytes:
        """Emit the trailing output sample (if one falls on the last input sample) and reset."""
        out = array("h")
        if self.src_rate != self.dst_rate and self._carry and self._pos < self.dst_rate:
            out.append(self._carry[0])
        self._carry = array("h")
        self._history = array("h")
        self._pos = 0
        return _to_bytes(out)

    def _smooth(self, samples: array) -> array:
        taps = self._taps
        buf = self._history + samples
        if len(buf) < taps:
            self._history = buf
            return array("h")
        out = array("h")
        window = sum(buf[:taps])
        out.append(window // taps)
        for i in range(taps, len(buf)):
            window += buf[i] - buf[i - taps]
            out.append(window // taps)
        self._history = buf[len(buf) - (taps - 1) :]
        return out


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """One-shot convenience over `Pcm16Resampler` (process + flush)."""
    resampler = Pcm16Resampler(src_rate, dst_rate)
    return resampler.process(pcm) + resampler.flush()


#: Below this RMS (of 32768) a chunk is never speech (about -44 dBFS).
_MIN_SPEECH_RMS = 200.0
#: Speech must be this many times louder than the tracked noise floor.
_SPEECH_OVER_NOISE = 2.5
#: Fraction of the gap to the current level the noise floor climbs per second.
_NOISE_RISE_PER_S = 0.05


class _Endpointer:
    """Energy-based end-of-utterance detection on the caller's PCM16 audio.

    The live model has no server VAD, so the client must commit each turn. A turn ends
    after `silence_ms` of quiet following speech, or at `max_turn_ms` regardless, so a room
    that never goes quiet still produces finals. "Quiet" is relative to a tracked noise
    floor that falls instantly and rises slowly: steady venue noise is not speech, but a
    sustained loud passage does not erase itself. Heuristic — tune via the provider.
    """

    def __init__(self, sample_rate: int, *, silence_ms: int, max_turn_ms: int) -> None:
        self._rate = sample_rate
        self.silence_ms = silence_ms
        self.max_turn_ms = max_turn_ms
        self._floor = 0.0
        self._turn_ms = 0.0
        self._quiet_ms = 0.0
        self._heard_speech = False

    def feed(self, pcm: bytes) -> bool:
        """Account for one chunk; True means "commit the turn now"."""
        samples = _to_samples(pcm)
        n = len(samples)
        if n == 0:
            return False
        ms = n * 1000.0 / self._rate
        rms = math.sqrt(sum(s * s for s in samples) / n)
        if rms < self._floor:
            self._floor = rms
        else:
            self._floor += (rms - self._floor) * min(1.0, ms / 1000.0 * _NOISE_RISE_PER_S)
        speaking = rms > max(_MIN_SPEECH_RMS, self._floor * _SPEECH_OVER_NOISE)

        self._turn_ms += ms
        if speaking:
            self._heard_speech = True
            self._quiet_ms = 0.0
        else:
            self._quiet_ms += ms
        if (self._heard_speech and self._quiet_ms >= self.silence_ms) or self._turn_ms >= self.max_turn_ms:
            self._turn_ms = self._quiet_ms = 0.0
            self._heard_speech = False
            return True
        return False


# =====================================================================================
# Provider
# =====================================================================================


class OpenAIRealtimeProvider(TranscriptionProvider):
    """Streaming STT over OpenAI's realtime transcription WebSocket.

    `url` overrides the full endpoint (tests point it at a local fake). `delay` is the
    model's latency/accuracy knob ("minimal" … "xhigh"; None omits it). `noise_reduction`
    is "near_field", "far_field" or None. `silence_ms` / `max_turn_ms` tune the client-side
    endpointer that decides when a turn is committed (= when a final is produced).
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        url: str | None = None,
        model: str = DEFAULT_MODEL,
        delay: str | None = "low",
        prompt: str | None = None,
        noise_reduction: str | None = "near_field",
        silence_ms: int = 500,
        max_turn_ms: int = 15_000,
        connect_timeout: float = 10.0,
        finish_timeout: float = 5.0,
    ) -> None:
        if not api_key:
            raise TranscriptionError("OpenAI API key is empty")
        self._api_key = api_key
        self.url = url or DEFAULT_URL
        self.model = model
        self.delay = delay
        self.prompt = prompt
        self.noise_reduction = noise_reduction
        self.silence_ms = silence_ms
        self.max_turn_ms = max_turn_ms
        self.connect_timeout = connect_timeout
        self.finish_timeout = finish_timeout

    async def open(self, config: TranscriptionConfig) -> TranscriptionStream:
        if config.encoding != "pcm16":
            raise TranscriptionError(f"OpenAI realtime provider needs pcm16 audio, got {config.encoding!r}")
        try:
            ws = await connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                open_timeout=self.connect_timeout,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            hint = " (check OPENAI_API_KEY)" if status in (401, 403) else ""
            raise TranscriptionError(f"OpenAI realtime connect failed: HTTP {status}{hint}") from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise TranscriptionError(f"OpenAI realtime connect failed: {exc}") from exc

        try:
            await self._configure(ws, config)
        except BaseException:
            with suppress(Exception):
                await ws.close()
            raise
        return _OpenAIStream(ws, config, self)

    async def _configure(self, ws: ClientConnection, config: TranscriptionConfig) -> None:
        """Send `session.update` and wait for the server's acknowledgement."""
        update = _session_update(
            config, model=self.model, delay=self.delay, prompt=self.prompt, noise_reduction=self.noise_reduction
        )
        try:
            await ws.send(json.dumps(update))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.connect_timeout)
                event = json.loads(raw)
                kind = event.get("type")
                if kind in _EV_SESSION_UPDATED:
                    return
                if kind == _EV_ERROR:
                    raise TranscriptionError(f"OpenAI rejected the session configuration: {_error_text(event)}")
        except ConnectionClosed as exc:
            raise TranscriptionError(f"OpenAI closed the connection during setup: {exc}") from exc
        except TimeoutError as exc:
            raise TranscriptionError("OpenAI did not acknowledge the session configuration in time") from exc
        except (ValueError, TypeError) as exc:  # malformed JSON / unexpected shape
            raise TranscriptionError(f"OpenAI sent an unreadable message during setup: {exc}") from exc


def _error_text(event: dict[str, Any]) -> str:
    error = event.get("error") or {}
    message = error.get("message") or "unknown error"
    code = error.get("code")
    return f"{message} [{code}]" if code else str(message)


class _OpenAIStream(TranscriptionStream):
    """One transcription session. Audio in at `config.sample_rate`, events out."""

    def __init__(self, ws: ClientConnection, config: TranscriptionConfig, provider: OpenAIRealtimeProvider) -> None:
        self._ws = ws
        self._config = config
        self._finish_timeout = provider.finish_timeout
        self._resampler = Pcm16Resampler(config.sample_rate, API_SAMPLE_RATE)
        self._endpointer = _Endpointer(
            config.sample_rate, silence_ms=provider.silence_ms, max_turn_ms=provider.max_turn_ms
        )
        self._odd = b""  # a trailing byte when a chunk split a sample
        self._queue: asyncio.Queue[TranscriptEvent | TranscriptionError | None] = asyncio.Queue()
        self._partials: dict[str, str] = {}  # item_id -> interim text so far
        self._spans: deque[tuple[int, int]] = deque()  # (start_ms, end_ms) of commits awaiting an item_id
        self._item_spans: dict[str, tuple[int, int]] = {}
        self._pending = 0  # commits awaiting completed/failed
        self._idle = asyncio.Event()
        self._idle.set()
        self._sent_ms = 0.0
        self._turn_start_ms = 0.0
        self._uncommitted_ms = 0.0
        self._closed = False
        self._failure: TranscriptionError | None = None
        self._reader = asyncio.create_task(self._read_loop(), name="openai-stt-reader")

    # -- TranscriptionStream -------------------------------------------------

    async def send_audio(self, chunk: bytes) -> None:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise TranscriptionError("transcription stream is closed")
        data = self._odd + chunk
        cut = len(data) - len(data) % 2
        self._odd, data = data[cut:], data[:cut]
        if not data:
            return
        try:
            await self._ws.send(_append_event(self._resampler.process(data)))
        except ConnectionClosed as exc:
            raise self._fail(f"OpenAI connection closed while sending audio: {exc}") from exc
        ms = len(data) / 2 * 1000.0 / self._config.sample_rate
        self._sent_ms += ms
        self._uncommitted_ms += ms
        if self._endpointer.feed(data):
            await self._commit()

    def events(self) -> AsyncIterator[TranscriptEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, TranscriptionError):
                raise item
            yield item

    async def finish(self) -> None:
        """Commit the open turn, wait for its final (bounded by `finish_timeout`), then close."""
        if self._closed:
            return
        try:
            if self._failure is None:
                await self._commit()
                if self._pending:
                    try:
                        await asyncio.wait_for(self._idle.wait(), timeout=self._finish_timeout)
                    except TimeoutError:
                        log.warning("OpenAI transcription: %d turn(s) still pending at finish", self._pending)
        except ConnectionClosed:
            pass  # the reader reports the drop
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader.cancel()
        with suppress(asyncio.CancelledError):
            await self._reader
        with suppress(Exception):
            await self._ws.close()
        self._queue.put_nowait(None)

    # -- internals ---------------------------------------------------------

    async def _commit(self) -> None:
        if self._uncommitted_ms < _MIN_COMMIT_MS:
            return
        await self._ws.send(_COMMIT_EVENT)
        self._spans.append((round(self._turn_start_ms), round(self._sent_ms)))
        self._turn_start_ms = self._sent_ms
        self._uncommitted_ms = 0.0
        self._pending += 1
        self._idle.clear()

    def _turn_done(self, item_id: str) -> tuple[int, int] | None:
        self._partials.pop(item_id, None)
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._idle.set()
        return self._item_spans.pop(item_id, None)

    def _fail(self, message: str) -> TranscriptionError:
        if self._failure is None:
            self._failure = TranscriptionError(message)
            self._queue.put_nowait(self._failure)
        self._idle.set()
        return self._failure

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                self._handle(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except TranscriptionError as exc:
            self._fail(str(exc))
        except ConnectionClosed as exc:
            if not self._closed:
                self._fail(f"OpenAI connection dropped: {exc}")
        except Exception as exc:  # malformed message or unexpected shape
            self._fail(f"OpenAI sent an unreadable message: {exc}")
        else:
            if not self._closed:
                self._fail("OpenAI closed the connection")
        finally:
            self._idle.set()

    def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == _EV_DELTA:
            item_id = event.get("item_id") or ""
            text = self._partials.get(item_id, "") + (event.get("delta") or "")
            self._partials[item_id] = text
            if self._config.interim_results and text.strip():
                self._queue.put_nowait(TranscriptEvent(text=text, is_final=False))
        elif kind == _EV_COMPLETED:
            item_id = event.get("item_id") or ""
            span = self._turn_done(item_id)
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                start, end = span if span else (None, None)
                self._queue.put_nowait(TranscriptEvent(text=transcript, is_final=True, start_ms=start, end_ms=end))
        elif kind == _EV_FAILED:
            # One turn failed; the session stays usable, so log and carry on.
            log.warning("OpenAI transcription of one turn failed: %s", _error_text(event))
            self._turn_done(event.get("item_id") or "")
        elif kind == _EV_COMMITTED:
            if self._spans:
                self._item_spans[event.get("item_id") or ""] = self._spans.popleft()
        elif kind == _EV_ERROR:
            raise TranscriptionError(f"OpenAI realtime error: {_error_text(event)}")
