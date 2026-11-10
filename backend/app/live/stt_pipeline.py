"""Tee of the speaker's audio into streaming speech-to-text.

`SttPipeline.start()` is called when a speaker stream starts. It resolves the session's
provider through `app.transcription.factory`, opens a `TranscriptionStream` in a background
task, forwards PCM16 audio and hands every `TranscriptEvent` to `live_service.ingest_transcript`.

The audio path is never blocked: `SttSession.feed()` is synchronous and drops the *oldest*
audio for STT when the provider cannot keep up. Provider failures trigger a bounded reconnect
with exponential backoff; when the budget is spent the session publishes an `error` state and
the audio broadcast simply continues without a transcript.

Published states (`session.stt.state`, data `{provider, status, error}`):
connecting -> active -> (connecting -> active)* -> stopped, or -> error.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.core.context import AppContext
from app.live import service as live_service
from app.live.audio_hub import AudioFormat, BoundedAudioQueue
from app.observability import metrics
from app.realtime import events as ev
from app.realtime.events import session_topic
from app.schemas.live import SessionSettings
from app.transcription import factory
from app.transcription.base import (
    TranscriptEvent,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionStream,
)

log = logging.getLogger(__name__)

STT_BUFFER_MS = 2000  # audio kept for STT while the provider is slow or reconnecting
STT_CHUNK_MS = 100  # audio is coalesced into chunks of at least this length before send_audio()
STOP_GRACE_S = 3.0  # how long a stopping session waits for the provider's final results


class SttSession:
    """Transcription of one speaker stream. Created by `SttPipeline.start`, ended by `stop()`."""

    def __init__(
        self,
        pipeline: SttPipeline,
        session_id: str,
        provider: TranscriptionProvider,
        config: TranscriptionConfig,
        fmt: AudioFormat,
    ) -> None:
        self._pipeline = pipeline
        self._ctx = pipeline._ctx
        self.session_id = session_id
        self.provider = provider
        self.config = config
        self.status = "connecting"
        self.last_error: str | None = None
        self.reconnects = 0
        self.events_received = 0
        self._queue = BoundedAudioQueue(STT_BUFFER_MS * fmt.bytes_per_ms)
        self._chunk_bytes = int(STT_CHUNK_MS * fmt.bytes_per_ms)
        self._stop_requested = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        # perf_counter() at the first send_audio of the current provider stream = audio position 0 of
        # that stream; provider `end_ms` offsets are measured from it for `evently.stt.latency`.
        self._stream_t0: float | None = None

    @property
    def audio_dropped(self) -> int:
        """Chunks dropped because STT fell behind (listeners were unaffected)."""
        return self._queue.dropped

    def feed(self, payload: bytes) -> None:
        """Queue PCM16 audio for transcription. Never blocks; drops oldest audio when the provider backs up."""
        self._queue.push(payload, len(payload))

    async def stop(self, *, grace: float = STOP_GRACE_S) -> None:
        """Flush, let the provider emit its final results for up to `grace` seconds, then close. Idempotent."""
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_requested.set()
            self._queue.close()
            try:
                if self._task is not None:
                    try:
                        await asyncio.wait_for(self._task, grace)
                    except TimeoutError:
                        log.warning("stt session=%s did not finish within %.1fs; closed", self.session_id, grace)
                    except Exception:  # noqa: BLE001 - runner failures were already reported
                        log.exception("stt runner failed session=%s", self.session_id)
            finally:
                # Also reached when the caller itself is cancelled (server shutdown): the runner
                # closes its provider stream in its own finally block.
                if self._task is not None and not self._task.done():
                    self._task.cancel()
                self._pipeline._forget(self)
                if self.status != "error":
                    await self._publish("stopped")

    def _start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"stt:{self.session_id}")

    # -- runner -----------------------------------------------------------------

    async def _run(self) -> None:
        failures = 0
        while not self._stop_requested.is_set():
            await self._publish("connecting")
            opened_at = time.monotonic()
            events_before = self.events_received
            stream: TranscriptionStream | None = None
            self._stream_t0 = None
            try:
                stream = await self.provider.open(self.config)
                await self._publish("active")
                await self._pump(stream)
            except TranscriptionError as exc:
                self.last_error = str(exc) or exc.__class__.__name__
            except Exception as exc:  # noqa: BLE001 - a provider bug must not kill the audio path
                log.exception("stt provider %s crashed session=%s", self.provider.name, self.session_id)
                self.last_error = f"{exc.__class__.__name__}: {exc}"
            else:
                if self._stop_requested.is_set():
                    return
                self.last_error = "transcription stream ended unexpectedly"
            finally:
                if stream is not None:
                    await self._close_quietly(stream)
            if self._stop_requested.is_set():
                return
            # A stream that produced results or lived a while was healthy: its loss does not count
            # against the reconnect budget, so long-running sessions survive periodic provider resets.
            if self.events_received > events_before or time.monotonic() - opened_at >= self._pipeline.stable_after_s:
                failures = 0
            failures += 1
            if failures > self._pipeline.max_reconnects:
                log.error("stt giving up session=%s after %d failures: %s", self.session_id, failures, self.last_error)
                await self._publish("error")
                self._queue.close()
                return
            self.reconnects += 1
            delay = min(self._pipeline.backoff_max_s, self._pipeline.backoff_base_s * 2 ** (failures - 1))
            log.warning("stt reconnect %d session=%s in %.2fs: %s", failures, self.session_id, delay, self.last_error)
            try:
                await asyncio.wait_for(self._stop_requested.wait(), delay)
            except TimeoutError:
                continue

    async def _pump(self, stream: TranscriptionStream) -> None:
        """Run audio sending and event receiving together until the stream ends or one side fails."""
        sender = asyncio.create_task(self._send_audio(stream))
        receiver = asyncio.create_task(self._receive_events(stream))
        try:
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if sender.done() and sender.exception() is None and not receiver.done():
                # Audio finished cleanly (stop requested, finish() sent): wait for the final results.
                await asyncio.wait({receiver})
            for task in (sender, receiver):
                if task.done() and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
        finally:
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    async def _send_audio(self, stream: TranscriptionStream) -> None:
        pending: list[bytes] = []
        pending_len = 0
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            pending.append(chunk)
            pending_len += len(chunk)
            # Coalesce the speaker's small frames: providers want roughly 50–250 ms per send.
            if pending_len >= self._chunk_bytes and not len(self._queue):
                if self._stream_t0 is None:
                    self._stream_t0 = time.perf_counter()
                await stream.send_audio(b"".join(pending))
                pending, pending_len = [], 0
        if pending:
            if self._stream_t0 is None:
                self._stream_t0 = time.perf_counter()
            await stream.send_audio(b"".join(pending))
        await stream.finish()

    async def _receive_events(self, stream: TranscriptionStream) -> None:
        async for event in stream.events():
            self.events_received += 1
            t0 = self._stream_t0
            if t0 is not None and event.end_ms is not None:
                # now - (stream start + end of the audio this segment covers); see docs/OBSERVABILITY.md §4.
                metrics.stt_latency(
                    self.provider.name, time.perf_counter() - t0 - event.end_ms / 1000, final=event.is_final
                )
            await self._ingest(event)

    async def _ingest(self, event: TranscriptEvent) -> None:
        try:
            await live_service.ingest_transcript(self._ctx, self.session_id, event, source="stt")
        except Exception:  # noqa: BLE001 - a transcript consumer bug must not drop the STT connection
            log.exception("ingest_transcript failed session=%s", self.session_id)

    async def _close_quietly(self, stream: TranscriptionStream) -> None:
        try:
            await stream.close()
        except Exception:  # noqa: BLE001
            log.debug("stt stream close failed session=%s", self.session_id, exc_info=True)

    async def _publish(self, status: str) -> None:
        self.status = status
        await publish_stt_state(self._ctx, self.session_id, self.provider.name, status, self.last_error)


async def publish_stt_state(ctx: AppContext, session_id: str, provider: str, status: str, error: str | None) -> None:
    await ctx.bus.publish(
        session_topic(session_id),
        ev.SESSION_STT_STATE,
        {"provider": provider, "status": status, "error": error},
    )


class SttPipeline:
    """App-scoped owner of the per-session transcription tasks."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        max_reconnects: int = 4,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 5.0,
        stable_after_s: float = 30.0,
    ) -> None:
        self._ctx = ctx
        self.max_reconnects = max_reconnects
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.stable_after_s = stable_after_s
        self._sessions: dict[str, SttSession] = {}

    def active(self, session_id: str) -> SttSession | None:
        return self._sessions.get(session_id)

    async def start(self, session_id: str, fmt: AudioFormat, settings: SessionSettings) -> SttSession | None:
        """Begin transcribing a speaker stream; returns None when there is nothing to do server-side.

        Browser mode (`stt_provider` resolves to no provider) is a silent no-op. A non-PCM16
        stream or an unusable provider publishes an `error` state and returns None. Any
        transcription already running for the session is stopped first.
        """
        previous = self._sessions.pop(session_id, None)
        if previous is not None:
            await previous.stop(grace=0.5)
        try:
            provider = factory.get_transcription_provider(self._ctx.settings, settings.stt_provider)
        except Exception as exc:  # noqa: BLE001 - TranscriptionError (no key) or a factory bug
            log.warning("stt provider %r unavailable session=%s: %s", settings.stt_provider, session_id, exc)
            await publish_stt_state(self._ctx, session_id, settings.stt_provider, "error", str(exc))
            return None
        if provider is None:
            return None
        if fmt.codec != "pcm16":
            message = f"server-side transcription needs pcm16 audio; this stream is {fmt.codec}"
            await publish_stt_state(self._ctx, session_id, provider.name, "error", message)
            return None
        config = TranscriptionConfig(
            sample_rate=fmt.sample_rate,
            encoding="pcm16",
            language=settings.language,
            keyterms=await self._keyterms(session_id),
        )
        session = SttSession(self, session_id, provider, config, fmt)
        self._sessions[session_id] = session
        session._start()
        return session

    async def _keyterms(self, session_id: str) -> list[str]:
        try:
            return list(await live_service.get_keyterms(self._ctx, session_id))
        except Exception:  # noqa: BLE001 - keyword boosting is an optimisation, never a blocker
            log.exception("get_keyterms failed session=%s; transcribing without keyterms", session_id)
            return []

    def _forget(self, session: SttSession) -> None:
        if self._sessions.get(session.session_id) is session:
            del self._sessions[session.session_id]

    async def close(self) -> None:
        """Stop every transcription immediately (app shutdown)."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(s.stop(grace=0.5) for s in sessions), return_exceptions=True)


def get_stt_pipeline(ctx: AppContext) -> SttPipeline:
    """The app's single `SttPipeline`, created on first use and closed on shutdown."""

    def build() -> SttPipeline:
        pipeline = SttPipeline(ctx)
        ctx.on_shutdown(pipeline.close)
        return pipeline

    return ctx.service("stt_pipeline", build)
