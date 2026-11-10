"""AudioHub: frame header, validation, fan-out, drop policy, exclusivity, cap, state envelopes."""

from __future__ import annotations

import asyncio

import pytest

from app.live.audio_hub import (
    CODEC_OPUS,
    CODEC_PCM16,
    HEADER_SIZE,
    AudioFormat,
    AudioHub,
    InvalidFrame,
    ListenerLimitReached,
    SourceBusy,
    UnsupportedFormat,
    get_audio_hub,
    pack_header,
    unpack_header,
)
from app.realtime import events as ev
from tests.helpers import session_published
from tests.test_audio_fakes import PCM_20MS, SESSION_ID, wait_until

PCM16K = AudioFormat(codec="pcm16", sample_rate=16000, channels=1, frame_ms=20)


def frame(seq: int, payload: bytes = PCM_20MS, *, sample_rate: int = 16000, codec: int = CODEC_PCM16) -> bytes:
    return pack_header(seq, seq * 20, sample_rate, codec) + payload


def hub_for(ctx, **kwargs) -> AudioHub:
    kwargs.setdefault("max_listeners", 10)
    kwargs.setdefault("state_debounce_s", 0.02)
    return AudioHub(ctx.bus, **kwargs)


async def start(hub: AudioHub, **kwargs):
    kwargs.setdefault("participant_id", "prt_speaker")
    kwargs.setdefault("is_host", False)
    return await hub.start_source(SESSION_ID, PCM16K, **kwargs)


# --- header + validation -------------------------------------------------------


def test_header_pack_unpack_roundtrip():
    header = pack_header(7, 140, 16000, CODEC_PCM16, flags=3)
    assert len(header) == HEADER_SIZE == 12
    assert header == bytes.fromhex("00000007 0000008c 00a0 00 03".replace(" ", ""))
    parsed = unpack_header(header + b"xx")
    assert (parsed.seq, parsed.timestamp_ms, parsed.sample_rate, parsed.codec, parsed.flags) == (7, 140, 16000, 0, 3)
    # Counters wrap at 2**32 instead of failing.
    assert unpack_header(pack_header(2**32 + 5, 2**33 + 1, 48000, CODEC_OPUS)).seq == 5
    with pytest.raises(ValueError):
        pack_header(0, 0, 22050, CODEC_PCM16)
    with pytest.raises(InvalidFrame):
        unpack_header(b"\x00" * 11)


def test_frame_validation_against_declared_format():
    assert PCM16K.check_frame(frame(1)).seq == 1
    assert PCM16K.check_frame(frame(2, b"\x00\x00" * 16 * 120)).timestamp_ms == 40  # 120 ms: the maximum
    with pytest.raises(InvalidFrame, match="no payload"):
        PCM16K.check_frame(frame(1, b""))
    with pytest.raises(InvalidFrame, match="exceeds"):
        PCM16K.check_frame(frame(1, b"\x00\x00" * 16 * 121))
    with pytest.raises(InvalidFrame, match="codec"):
        PCM16K.check_frame(frame(1, codec=CODEC_OPUS))
    with pytest.raises(InvalidFrame, match="sample rate"):
        PCM16K.check_frame(frame(1, sample_rate=48000))
    with pytest.raises(InvalidFrame, match="whole samples"):
        PCM16K.check_frame(frame(1, b"\x00" * 641))
    assert PCM16K.payload_duration_ms(len(PCM_20MS)) == 20.0
    opus = AudioFormat(codec="opus", sample_rate=48000, frame_ms=40)
    assert opus.payload_duration_ms(100) == 40.0 and opus.max_payload_bytes == 4096


def test_unsupported_formats_are_refused():
    for fmt in (
        AudioFormat(codec="mp3"),
        AudioFormat(sample_rate=22050),
        AudioFormat(channels=2),
        AudioFormat(frame_ms=5),
        AudioFormat(frame_ms=500),
    ):
        with pytest.raises(UnsupportedFormat):
            fmt.validate()
    AudioFormat(codec="opus", sample_rate=48000, frame_ms=60).validate()


# --- fan-out ---------------------------------------------------------------------


async def test_frames_fan_out_to_every_listener_in_order(ctx):
    hub = hub_for(ctx)
    early = hub.add_listener(SESSION_ID)
    source = await start(hub)
    late = hub.add_listener(SESSION_ID)
    frames = [frame(i) for i in range(5)]
    for f in frames:
        source.publish(f)
    await hub.stop_source(source)

    for listener in (early, late):
        assert await listener.get() == {
            "type": "format",
            "codec": "pcm16",
            "sample_rate": 16000,
            "channels": 1,
            "frame_ms": 20,
        }
        assert [await listener.get() for _ in frames] == frames
        assert await listener.get() == {"type": "stopped", "reason": "source_left"}
    assert hub.stats(SESSION_ID)["frames_in"] == 0  # the source is gone; counters went with it
    assert source.frames_in == 5 and source.bytes_in == 5 * len(frames[0])


async def test_invalid_frames_are_rejected_and_counted(ctx):
    hub = hub_for(ctx)
    source = await start(hub)
    listener = hub.add_listener(SESSION_ID)
    with pytest.raises(InvalidFrame):
        source.publish(frame(1, sample_rate=8000))
    source.publish(frame(2))
    assert source.frames_rejected == 1 and source.frames_in == 1
    assert await listener.get() == {"type": "format", **PCM16K.as_message()}
    assert await listener.get() == frame(2)


async def test_slow_listener_drops_oldest_and_stays_within_latency_bound(ctx):
    hub = hub_for(ctx, listener_buffer_ms=500)
    source = await start(hub)
    slow = hub.add_listener(SESSION_ID)
    fast = hub.add_listener(SESSION_ID)
    received_fast: list[bytes] = []

    async def drain_fast():
        while isinstance(item := await fast.get(), bytes | dict):
            if isinstance(item, bytes):
                received_fast.append(item)

    drainer = asyncio.create_task(drain_fast())
    frames = [frame(i) for i in range(40)]  # 800 ms of audio, published in one burst
    for f in frames:
        source.publish(f)
        await asyncio.sleep(0)  # let the fast listener's sender run between frames

    assert slow.buffered_ms <= 500 and slow.dropped == 15
    assert await slow.get() == {"type": "format", **PCM16K.as_message()}
    kept = [await slow.get() for _ in range(25)]
    assert kept == frames[15:]  # the newest half second survives; the oldest frames went
    await wait_until(lambda: len(received_fast) == 40)
    assert received_fast == frames and fast.dropped == 0
    assert hub.stats(SESSION_ID)["frames_dropped"] == 15
    hub.remove_listener(slow)
    assert hub.stats(SESSION_ID)["frames_dropped"] == 15  # remembered after the listener left
    drainer.cancel()


async def test_control_messages_survive_overflow(ctx):
    hub = hub_for(ctx, listener_buffer_ms=100)
    source = await start(hub)
    listener = hub.add_listener(SESSION_ID)
    for i in range(10):
        source.publish(frame(i))
    listener.push_control({"type": "pong"})
    for i in range(10, 20):
        source.publish(frame(i))
    items = [await listener.get() for _ in range(len(listener._queue))]
    assert items[0] == {"type": "format", **PCM16K.as_message()}
    assert {"type": "pong"} in items
    assert sum(isinstance(item, bytes) for item in items) == 5 and items[-1] == frame(19)


# --- exclusivity ------------------------------------------------------------------


async def test_second_source_refused_unless_host_takeover(ctx):
    hub = hub_for(ctx)
    first = await start(hub, participant_id="prt_a")
    listener = hub.add_listener(SESSION_ID)
    assert await listener.get() == {"type": "format", **PCM16K.as_message()}

    with pytest.raises(SourceBusy) as busy:
        await start(hub, participant_id="prt_b")
    assert busy.value.source_participant_id == "prt_a"
    with pytest.raises(SourceBusy):
        await start(hub, participant_id="prt_b", takeover=True)  # explicit, but not a host
    assert not first.closed

    second = await start(hub, participant_id="prt_host", is_host=True, takeover=True)
    assert first.closed and await first.wait_closed() == "takeover"
    assert await listener.get() == {"type": "stopped", "reason": "takeover"}
    assert await listener.get() == {"type": "format", **PCM16K.as_message()}
    first.publish(frame(1))  # the evicted speaker's late frames go nowhere
    second.publish(frame(1))
    assert await listener.get() == frame(1) and len(listener._queue) == 0

    await hub.stop_source(first)  # late cleanup by the evicted connection must not disturb the new source
    assert hub.stats(SESSION_ID)["broadcasting"] is True
    await hub.stop_source(second)
    assert await listener.get() == {"type": "stopped", "reason": "source_left"}


async def test_same_participant_may_take_over_its_own_stale_stream(ctx):
    hub = hub_for(ctx)
    stale = await start(hub, participant_id="prt_a")
    fresh = await start(hub, participant_id="prt_a", takeover=True)
    assert stale.closed and not fresh.closed


async def test_listener_cap_is_enforced(ctx):
    hub = hub_for(ctx, max_listeners=2)
    first, second = hub.add_listener(SESSION_ID), hub.add_listener(SESSION_ID)
    with pytest.raises(ListenerLimitReached):
        hub.add_listener(SESSION_ID)
    hub.remove_listener(first)
    hub.add_listener(SESSION_ID)
    assert hub.stats(SESSION_ID)["listeners"] == 2
    hub.remove_listener(second)
    hub.remove_listener(second)  # idempotent
    assert second.closed and await second.get() is None


# --- state envelopes ---------------------------------------------------------------


def audio_states(ctx) -> list[dict]:
    return [e.data for e in session_published(ctx, SESSION_ID, ev.SESSION_AUDIO_STATE)]


async def test_audio_state_is_published_on_start_stop_and_debounced_listener_changes(ctx):
    hub = hub_for(ctx, state_debounce_s=0.05)
    source = await start(hub)
    assert audio_states(ctx) == [
        {
            "broadcasting": True,
            "source_participant_id": "prt_speaker",
            "listeners": 0,
            "codec": "pcm16",
            "sample_rate": 16000,
        }
    ]
    listeners = [hub.add_listener(SESSION_ID) for _ in range(3)]
    hub.remove_listener(listeners.pop())
    assert len(audio_states(ctx)) == 1  # nothing yet: debounced
    await wait_until(lambda: len(audio_states(ctx)) == 2)
    assert audio_states(ctx)[-1]["listeners"] == 2
    await asyncio.sleep(0.1)
    assert len(audio_states(ctx)) == 2  # no churn, no envelope

    await hub.stop_source(source)
    assert audio_states(ctx)[-1] == {
        "broadcasting": False,
        "source_participant_id": None,
        "listeners": 2,
        "codec": None,
        "sample_rate": None,
    }
    for listener in listeners:
        hub.remove_listener(listener)
    await wait_until(lambda: audio_states(ctx)[-1]["listeners"] == 0)
    assert hub.stats(SESSION_ID)["listeners"] == 0 and SESSION_ID not in hub._rooms  # idle rooms are forgotten


async def test_broadcast_can_be_disabled_without_stopping_the_source(ctx):
    hub = hub_for(ctx)
    listener = hub.add_listener(SESSION_ID)
    source = await start(hub, broadcast=False)
    source.publish(frame(1))
    assert len(listener._queue) == 0 and source.frames_in == 1
    assert (
        audio_states(ctx)[-1]["broadcasting"] is False
        and audio_states(ctx)[-1]["source_participant_id"] == "prt_speaker"
    )

    await hub.set_broadcast(source, True)
    source.publish(frame(2))
    assert await listener.get() == {"type": "format", **PCM16K.as_message()}
    assert await listener.get() == frame(2)
    assert audio_states(ctx)[-1]["broadcasting"] is True

    await hub.set_broadcast(source, False)
    assert await listener.get() == {"type": "stopped", "reason": "broadcast_disabled"}
    await hub.set_broadcast(source, False)  # no change, no envelope
    assert [s["broadcasting"] for s in audio_states(ctx)] == [False, True, False]
    await hub.stop_source(source)
    assert len(listener._queue) == 0  # already told it stopped


async def test_close_releases_sources_and_listeners(ctx):
    hub = hub_for(ctx)
    source = await start(hub)
    listener = hub.add_listener(SESSION_ID)
    await listener.get()
    await hub.close()
    assert await source.wait_closed() == "shutdown"
    assert await listener.get() is None
    assert hub.stats(SESSION_ID)["listeners"] == 0


async def test_get_audio_hub_is_an_app_singleton_with_a_shutdown_hook(ctx):
    hub = get_audio_hub(ctx)
    assert get_audio_hub(ctx) is hub
    assert hub._max_listeners == ctx.settings.audio_max_listeners_per_session
    assert any(getattr(hook, "__self__", None) is hub for hook in ctx._shutdown_hooks)
