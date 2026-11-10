"""In-memory audio fan-out: one speaker per live session, many listeners.

Frames are opaque bytes: the 12-byte header from the implementation plan (§4) followed by the
payload. The hub validates frames against the stream's declared `AudioFormat`, relays them
verbatim and never interprets the payload.

Latency rules:

* `AudioSource.publish()` is synchronous and O(listeners): it appends the frame to each
  listener's queue and wakes its sender. Nothing in the hub awaits network I/O.
* Every listener queue is bounded in *milliseconds of audio* (`listener_buffer_ms`); when a
  listener's socket cannot keep up, its oldest frames are dropped and counted. A listener
  therefore never lags more than about half a second behind the speaker inside the server,
  and never slows the speaker or other listeners down.

Control messages for listeners (`format`, `stopped`) travel through the same queue as the
frames so they arrive in order, and are never dropped.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.core.context import AppContext
from app.observability import metrics
from app.realtime import events as ev
from app.realtime.bus import EventBus
from app.realtime.events import session_topic

log = logging.getLogger(__name__)

# --- frame header -----------------------------------------------------------
# uint32 seq, uint32 timestamp_ms, uint16 sample_rate_hz/100, uint8 codec, uint8 flags; big-endian.
_HEADER = struct.Struct(">IIHBB")
HEADER_SIZE = _HEADER.size  # 12

CODEC_PCM16 = 0  # PCM signed 16-bit little-endian
CODEC_OPUS = 1  # Opus packets (relay only; cannot be transcribed server-side)
CODEC_IDS = {"pcm16": CODEC_PCM16, "opus": CODEC_OPUS}
CODEC_NAMES = {v: k for k, v in CODEC_IDS.items()}

SUPPORTED_SAMPLE_RATES = (8000, 16000, 24000, 32000, 44100, 48000)
MIN_FRAME_MS = 10
MAX_FRAME_MS = 120  # longest frame a speaker may send
MAX_ENCODED_PAYLOAD_BYTES = 4096  # sane cap for non-PCM codecs
LISTENER_BUFFER_MS = 500  # per-listener latency bound inside the server
_U32 = 0xFFFFFFFF


class InvalidFrame(ValueError):
    """A speaker frame does not match the header contract or the declared format."""


class UnsupportedFormat(ValueError):
    """The declared stream format cannot be relayed."""


class SourceBusy(RuntimeError):
    """Another speaker is active for this session and may not be replaced by this one."""

    def __init__(self, source_participant_id: str | None) -> None:
        super().__init__("another speaker is already streaming")
        self.source_participant_id = source_participant_id


class ListenerLimitReached(RuntimeError):
    """`audio_max_listeners_per_session` listeners are already connected."""


@dataclass(frozen=True, slots=True)
class FrameHeader:
    seq: int
    timestamp_ms: int
    sample_rate: int  # Hz
    codec: int
    flags: int = 0  # reserved, relayed verbatim


def pack_header(seq: int, timestamp_ms: int, sample_rate: int, codec: int, flags: int = 0) -> bytes:
    """Encode the 12-byte frame header. `seq`/`timestamp_ms` wrap at 2**32; `sample_rate` must be a multiple of 100."""
    if sample_rate % 100 or not 0 < sample_rate // 100 <= 0xFFFF:
        raise ValueError(f"sample rate {sample_rate} cannot be encoded in the frame header")
    return _HEADER.pack(seq & _U32, timestamp_ms & _U32, sample_rate // 100, codec, flags)


def unpack_header(frame: bytes) -> FrameHeader:
    """Decode the header of `frame`. Raises `InvalidFrame` when the frame is too short."""
    if len(frame) < HEADER_SIZE:
        raise InvalidFrame(f"frame is {len(frame)} bytes; the header alone is {HEADER_SIZE}")
    seq, ts, rate_100, codec, flags = _HEADER.unpack_from(frame)
    return FrameHeader(seq=seq, timestamp_ms=ts, sample_rate=rate_100 * 100, codec=codec, flags=flags)


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """What a speaker declared in its hello; every frame must agree with it."""

    codec: str = "pcm16"
    sample_rate: int = 16000
    channels: int = 1
    frame_ms: int = 20  # nominal frame length; the actual duration of PCM frames is derived from their size

    def validate(self) -> None:
        """Raise `UnsupportedFormat` unless the hub can relay this stream."""
        if self.codec not in CODEC_IDS:
            raise UnsupportedFormat(f"unsupported codec {self.codec!r}; use one of {sorted(CODEC_IDS)}")
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise UnsupportedFormat(f"unsupported sample rate {self.sample_rate}; use one of {SUPPORTED_SAMPLE_RATES}")
        if self.channels != 1:
            raise UnsupportedFormat("only mono audio is relayed")
        if not MIN_FRAME_MS <= self.frame_ms <= MAX_FRAME_MS:
            raise UnsupportedFormat(f"frame_ms must be between {MIN_FRAME_MS} and {MAX_FRAME_MS}")

    @property
    def codec_id(self) -> int:
        return CODEC_IDS[self.codec]

    @property
    def bytes_per_ms(self) -> float:
        """PCM16 only: payload bytes per millisecond of audio."""
        return self.sample_rate * 2 * self.channels / 1000

    @property
    def max_payload_bytes(self) -> int:
        if self.codec == "pcm16":
            return int(MAX_FRAME_MS * self.bytes_per_ms)
        return MAX_ENCODED_PAYLOAD_BYTES

    def payload_duration_ms(self, payload_len: int) -> float:
        """Audio duration carried by a payload: exact for PCM16, the declared `frame_ms` otherwise."""
        if self.codec == "pcm16":
            return payload_len / self.bytes_per_ms
        return float(self.frame_ms)

    def check_frame(self, frame: bytes) -> FrameHeader:
        """Validate a speaker frame against this format. Returns the parsed header or raises `InvalidFrame`."""
        header = unpack_header(frame)
        payload_len = len(frame) - HEADER_SIZE
        if payload_len == 0:
            raise InvalidFrame("frame has no payload")
        if payload_len > self.max_payload_bytes:
            raise InvalidFrame(f"payload of {payload_len} bytes exceeds the {self.max_payload_bytes}-byte maximum")
        if header.codec != self.codec_id:
            raise InvalidFrame(f"frame codec {header.codec} does not match the declared {self.codec!r}")
        if header.sample_rate != self.sample_rate:
            raise InvalidFrame(f"frame sample rate {header.sample_rate} does not match the declared {self.sample_rate}")
        if self.codec == "pcm16" and payload_len % (2 * self.channels):
            raise InvalidFrame("PCM16 payload is not whole samples")
        return header

    def as_message(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frame_ms": self.frame_ms,
        }


class BoundedAudioQueue:
    """Single-consumer queue of audio chunks with a cost budget (ms, bytes…) and drop-oldest overflow.

    Control items (cost 0) are never dropped. `get()` returns None once the queue is closed
    and drained.
    """

    def __init__(self, max_cost: float) -> None:
        self.max_cost = max_cost
        self.dropped = 0
        self.closed = False
        self._items: deque[tuple[Any, float]] = deque()
        self._cost = 0.0
        self._wakeup = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def buffered(self) -> float:
        """Total cost of the audio currently queued."""
        return self._cost

    def push(self, item: Any, cost: float = 0.0) -> None:
        if self.closed:
            return
        self._items.append((item, cost))
        self._cost += cost
        dropped = 0
        while self._cost > self.max_cost and self._drop_oldest_audio():
            dropped += 1
        if dropped:
            self.dropped += dropped
            metrics.audio_frames_dropped(dropped)
        self._wakeup.set()

    def _drop_oldest_audio(self) -> bool:
        for index, (_, cost) in enumerate(self._items):
            if cost > 0:
                del self._items[index]
                self._cost -= cost
                return True
        return False

    async def get(self) -> Any | None:
        while not self._items:
            if self.closed:
                return None
            self._wakeup.clear()
            await self._wakeup.wait()
        item, cost = self._items.popleft()
        self._cost -= cost
        if not self._items:
            self._cost = 0.0  # kill float drift once we are empty
        return item

    def close(self) -> None:
        self.closed = True
        self._wakeup.set()


class AudioListener:
    """One attendee device. Consume with `get()`: bytes are frames, dicts are JSON control messages, None = closed."""

    def __init__(self, session_id: str, buffer_ms: float) -> None:
        self.session_id = session_id
        self._queue = BoundedAudioQueue(buffer_ms)

    @property
    def dropped(self) -> int:
        return self._queue.dropped

    @property
    def buffered_ms(self) -> float:
        return self._queue.buffered

    @property
    def closed(self) -> bool:
        return self._queue.closed

    async def get(self) -> bytes | dict[str, Any] | None:
        return await self._queue.get()

    def push_control(self, message: dict[str, Any]) -> None:
        """Queue a JSON message for this listener, in order with the audio (e.g. a pong)."""
        self._queue.push(message)

    def _push_frame(self, frame: bytes, duration_ms: float) -> None:
        self._queue.push(frame, duration_ms)

    def _close(self) -> None:
        self._queue.close()


class AudioSource:
    """The speaker of a session. Obtained from `AudioHub.start_source`; feed frames with `publish()`."""

    def __init__(self, room: _Room, fmt: AudioFormat, participant_id: str | None, *, broadcast: bool) -> None:
        self._room = room
        self.format = fmt
        self.participant_id = participant_id
        self.broadcast = broadcast  # False = validate and count frames but relay nothing (audio_broadcast off)
        self.frames_in = 0
        self.bytes_in = 0
        self.frames_rejected = 0
        self.close_reason: str | None = None
        self._closed = asyncio.Event()

    @property
    def session_id(self) -> str:
        return self._room.session_id

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> str:
        """Block until the hub closes this source (takeover, shutdown or `stop_source`); returns the reason."""
        await self._closed.wait()
        return self.close_reason or "stopped"

    def publish(self, frame: bytes) -> FrameHeader:
        """Validate one speaker frame and fan it out to every listener without blocking.

        Raises `InvalidFrame` (the frame is dropped). Frames arriving after the source was
        closed are validated but not relayed.
        """
        try:
            header = self.format.check_frame(frame)
        except InvalidFrame:
            self.frames_rejected += 1
            raise
        self.frames_in += 1
        self.bytes_in += len(frame)
        metrics.audio_frames(self.format.codec, self.format.sample_rate)
        if self.closed or not self.broadcast:
            return header
        duration = self.format.payload_duration_ms(len(frame) - HEADER_SIZE)
        for listener in self._room.listeners:
            listener._push_frame(frame, duration)
        return header

    def _close(self, reason: str) -> None:
        if not self.closed:
            self.close_reason = reason
            self._closed.set()


@dataclass
class _Room:
    session_id: str
    source: AudioSource | None = None
    listeners: set[AudioListener] = field(default_factory=set)
    frames_dropped: int = 0  # by listeners that already left; live listeners are summed on demand
    state_task: asyncio.Task[None] | None = None
    last_state: dict[str, Any] | None = None

    @property
    def broadcasting(self) -> bool:
        return self.source is not None and self.source.broadcast

    @property
    def idle(self) -> bool:
        return self.source is None and not self.listeners and self.state_task is None


class AudioHub:
    """App-scoped registry of audio sources and listeners, keyed by live-session id."""

    def __init__(
        self,
        bus: EventBus,
        *,
        max_listeners: int,
        listener_buffer_ms: float = LISTENER_BUFFER_MS,
        state_debounce_s: float = 1.0,
    ) -> None:
        self._bus = bus
        self._max_listeners = max_listeners
        self._listener_buffer_ms = listener_buffer_ms
        self._state_debounce_s = state_debounce_s
        self._rooms: dict[str, _Room] = {}

    # -- sources ----------------------------------------------------------------

    async def start_source(
        self,
        session_id: str,
        fmt: AudioFormat,
        *,
        participant_id: str | None,
        is_host: bool,
        takeover: bool = False,
        broadcast: bool = True,
    ) -> AudioSource:
        """Register the speaker of `session_id`.

        If a speaker is already active, the new one is refused with `SourceBusy` unless it asks
        to `takeover` and is either a host or the same participant reconnecting; then the old
        source is closed (its `wait_closed()` returns "takeover") and listeners are told the
        stream stopped before the new one is announced. `UnsupportedFormat` is raised for formats
        the hub cannot relay.
        """
        fmt.validate()
        room = self._room(session_id)
        current = room.source
        if current is not None:
            same_participant = participant_id is not None and participant_id == current.participant_id
            if not (takeover and (is_host or same_participant)):
                raise SourceBusy(current.participant_id)
            self._detach_source(room, current, reason="takeover")
        source = AudioSource(room, fmt, participant_id, broadcast=broadcast)
        room.source = source
        if broadcast:
            self._notify_listeners(room, {"type": "format", **fmt.as_message()})
        log.info("audio source started session=%s participant=%s %s", session_id, participant_id, fmt)
        await self._publish_state(room)
        return source

    async def stop_source(self, source: AudioSource, *, reason: str = "source_left") -> None:
        """Unregister a speaker (idempotent). Listeners receive `stopped`; the audio state is published."""
        room = self._rooms.get(source.session_id)
        if room is None or room.source is not source:
            source._close(reason)
            return
        self._detach_source(room, source, reason=reason)
        log.info(
            "audio source stopped session=%s participant=%s frames=%d rejected=%d",
            source.session_id,
            source.participant_id,
            source.frames_in,
            source.frames_rejected,
        )
        await self._publish_state(room)
        self._discard_if_idle(room)

    async def set_broadcast(self, source: AudioSource, enabled: bool) -> None:
        """Turn relaying to listeners on or off mid-stream (the host toggled `audio_broadcast`)."""
        room = self._rooms.get(source.session_id)
        if source.closed or room is None or room.source is not source or source.broadcast == enabled:
            return
        source.broadcast = enabled
        if enabled:
            self._notify_listeners(room, {"type": "format", **source.format.as_message()})
        else:
            self._notify_listeners(room, {"type": "stopped", "reason": "broadcast_disabled"})
        await self._publish_state(room)

    def _detach_source(self, room: _Room, source: AudioSource, *, reason: str) -> None:
        was_broadcasting = room.broadcasting
        room.source = None
        source._close(reason)
        if was_broadcasting:
            self._notify_listeners(room, {"type": "stopped", "reason": reason})

    # -- listeners --------------------------------------------------------------

    def add_listener(self, session_id: str) -> AudioListener:
        """Register a listener. It immediately receives `format` when a stream is already running.

        Raises `ListenerLimitReached` at the configured cap.
        """
        room = self._room(session_id)
        if len(room.listeners) >= self._max_listeners:
            self._discard_if_idle(room)
            raise ListenerLimitReached(f"session {session_id} already has {self._max_listeners} listeners")
        listener = AudioListener(session_id, self._listener_buffer_ms)
        room.listeners.add(listener)
        metrics.audio_listener_joined()
        if room.broadcasting:
            listener.push_control({"type": "format", **room.source.format.as_message()})  # type: ignore[union-attr]
        self._schedule_state(room)
        return listener

    def remove_listener(self, listener: AudioListener) -> None:
        """Unregister a listener (idempotent) and close its queue."""
        listener._close()
        room = self._rooms.get(listener.session_id)
        if room is None or listener not in room.listeners:
            return
        room.listeners.discard(listener)
        room.frames_dropped += listener.dropped
        metrics.audio_listener_left()
        self._schedule_state(room)

    def _notify_listeners(self, room: _Room, message: dict[str, Any]) -> None:
        for listener in room.listeners:
            listener.push_control(message)

    # -- state + stats ----------------------------------------------------------

    def stats(self, session_id: str) -> dict[str, Any]:
        """Counters for one session (zeros when it has no audio activity)."""
        room = self._rooms.get(session_id)
        if room is None:
            return {
                "broadcasting": False,
                "listeners": 0,
                "frames_in": 0,
                "bytes_in": 0,
                "frames_rejected": 0,
                "frames_dropped": 0,
                "max_buffered_ms": 0.0,
            }
        source = room.source
        return {
            "broadcasting": room.broadcasting,
            "listeners": len(room.listeners),
            "frames_in": source.frames_in if source else 0,
            "bytes_in": source.bytes_in if source else 0,
            "frames_rejected": source.frames_rejected if source else 0,
            "frames_dropped": room.frames_dropped + sum(sub.dropped for sub in room.listeners),
            "max_buffered_ms": max((sub.buffered_ms for sub in room.listeners), default=0.0),
        }

    def _state(self, room: _Room) -> dict[str, Any]:
        source = room.source
        broadcasting = room.broadcasting
        return {
            "broadcasting": broadcasting,
            "source_participant_id": source.participant_id if source else None,
            "listeners": len(room.listeners),
            "codec": source.format.codec if broadcasting else None,
            "sample_rate": source.format.sample_rate if broadcasting else None,
        }

    async def _publish_state(self, room: _Room) -> None:
        if room.state_task is not None and room.state_task is not asyncio.current_task():
            room.state_task.cancel()
        room.state_task = None
        state = self._state(room)
        if state == room.last_state:
            return
        room.last_state = state
        await self._bus.publish(session_topic(room.session_id), ev.SESSION_AUDIO_STATE, state)

    def _schedule_state(self, room: _Room) -> None:
        """Listener counts change in bursts; publish them at most once per debounce window."""
        if room.state_task is None:
            room.state_task = asyncio.create_task(self._publish_state_later(room))

    async def _publish_state_later(self, room: _Room) -> None:
        await asyncio.sleep(self._state_debounce_s)
        await self._publish_state(room)
        self._discard_if_idle(room)

    # -- lifecycle --------------------------------------------------------------

    def _room(self, session_id: str) -> _Room:
        room = self._rooms.get(session_id)
        if room is None:
            room = self._rooms[session_id] = _Room(session_id)
        return room

    def _discard_if_idle(self, room: _Room) -> None:
        if room.idle and self._rooms.get(room.session_id) is room:
            del self._rooms[room.session_id]

    async def close(self) -> None:
        """Shut down: close every source and listener (their consumers see "shutdown" / None)."""
        rooms = list(self._rooms.values())
        self._rooms.clear()
        for room in rooms:
            if room.state_task is not None:
                room.state_task.cancel()
            if room.source is not None:
                room.source._close("shutdown")
            for listener in room.listeners:
                listener._close()
                metrics.audio_listener_left()
        pending = [r.state_task for r in rooms if r.state_task is not None]
        if pending:
            await asyncio.wait(pending)


def get_audio_hub(ctx: AppContext) -> AudioHub:
    """The app's single `AudioHub`, created on first use and closed on shutdown."""

    def build() -> AudioHub:
        hub = AudioHub(ctx.bus, max_listeners=ctx.settings.audio_max_listeners_per_session)
        ctx.on_shutdown(hub.close)
        return hub

    return ctx.service("audio_hub", build)
