"""WS /ws/sessions/{id}/audio: speaker and listener flows against a faked live service."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api import ws_audio
from app.live.audio_hub import CODEC_OPUS, CODEC_PCM16, get_audio_hub, pack_header
from app.realtime import events as ev
from app.realtime.events import session_topic
from tests.helpers import session_published
from tests.test_audio_fakes import PCM_20MS, SESSION_ID, FakeProvider, install_browser_mode, standard_live

HELLO = {"type": "hello", "codec": "pcm16", "sample_rate": 16000, "channels": 1, "frame_ms": 20}
FORMAT = {"type": "format", "codec": "pcm16", "sample_rate": 16000, "channels": 1, "frame_ms": 20}


def url(role: str, token: str | None, session_id: str = SESSION_ID) -> str:
    query = f"role={role}" + (f"&token={token}" if token else "")
    return f"/ws/sessions/{session_id}/audio?{query}"


def frame(seq: int, payload: bytes = PCM_20MS, codec: int = CODEC_PCM16) -> bytes:
    return pack_header(seq, seq * 20, 16000, codec) + payload


def expect_close(ws, code: int, error_code: str | None = None) -> None:
    """The server sends `{"type":"error"}` (when `error_code`) and then closes with `code`."""
    if error_code is not None:
        message = ws.receive_json()
        assert message["type"] == "error" and message["code"] == error_code, message
    with pytest.raises(WebSocketDisconnect) as disconnect:
        ws.receive_json()
    assert disconnect.value.code == code


def open_speaker(client, token: str = "speaker-token", **hello):
    ws = client.websocket_connect(url("speaker", token)).__enter__()
    ws.send_json({**HELLO, **hello})
    return ws


@pytest.fixture
def live(monkeypatch):
    install_browser_mode(monkeypatch)
    return standard_live(monkeypatch)


# --- admission -------------------------------------------------------------------


def test_bad_token_closes_4401(client, live):
    with client.websocket_connect(url("listener", "nope")) as ws:
        expect_close(ws, 4401, "unauthenticated")
    with client.websocket_connect(url("listener", None)) as ws:
        expect_close(ws, 4401, "unauthenticated")


def test_unknown_session_closes_4404(client, live):
    with client.websocket_connect(url("listener", "listener-token", session_id="ses_missing")) as ws:
        expect_close(ws, 4404, "unknown_session")


def test_listener_cannot_be_the_speaker(client, live):
    with client.websocket_connect(url("speaker", "listener-token")) as ws:
        expect_close(ws, 4403, "forbidden")


def test_session_not_live_closes_4409(client, live):
    live.sessions[SESSION_ID].status = "ended"
    with client.websocket_connect(url("speaker", "speaker-token")) as ws:
        expect_close(ws, 4409, "session_not_live")


def test_unknown_role_closes_4400(client, live):
    with client.websocket_connect(url("dj", "listener-token")) as ws:
        expect_close(ws, 4400, "bad_role")


# --- speaker + listener -----------------------------------------------------------


def test_speaker_streams_to_listeners_and_both_answer_pings(client, live):
    with client.websocket_connect(url("listener", "listener-token")) as early:
        with client.websocket_connect(url("speaker", "speaker-token")) as speaker:
            speaker.send_json(HELLO)
            ready = speaker.receive_json()
            assert ready == {**FORMAT, "type": "ready", "max_payload_bytes": 3840, "broadcast": True}
            assert early.receive_json() == FORMAT
            with client.websocket_connect(url("listener", "host-token")) as late:
                assert late.receive_json() == FORMAT  # a stream is already running
                frames = [frame(i) for i in range(3)]
                for f in frames:
                    speaker.send_bytes(f)
                assert [early.receive_bytes() for _ in frames] == frames
                assert [late.receive_bytes() for _ in frames] == frames
                speaker.send_json({"type": "ping", "t": 42})
                assert speaker.receive_json() == {"type": "pong", "t": 42}
                late.send_json({"type": "ping"})
                assert late.receive_json() == {"type": "pong"}
        # speaker left
        assert early.receive_json() == {"type": "stopped", "reason": "source_left"}

    ctx = client.app.state.ctx
    states = [e.data for e in session_published(ctx, SESSION_ID, ev.SESSION_AUDIO_STATE)]
    assert states[0] == {
        "broadcasting": True,
        "source_participant_id": "prt_speaker",
        "listeners": 1,
        "codec": "pcm16",
        "sample_rate": 16000,
    }
    assert states[-1]["broadcasting"] is False
    assert get_audio_hub(ctx).stats(SESSION_ID)["listeners"] == 0  # every registration cleaned up


def test_hello_must_come_first_and_be_valid(client, live):
    with client.websocket_connect(url("speaker", "speaker-token")) as ws:
        ws.send_bytes(frame(0))
        expect_close(ws, 4400, "invalid_hello")
    with client.websocket_connect(url("speaker", "speaker-token")) as ws:
        ws.send_json({**HELLO, "sample_rate": "fast"})
        expect_close(ws, 4400, "invalid_hello")
    with client.websocket_connect(url("speaker", "speaker-token")) as ws:
        ws.send_json({**HELLO, "channels": 2})
        expect_close(ws, 4400, "unsupported_format")


def test_hello_timeout(client, live, monkeypatch):
    monkeypatch.setattr(ws_audio, "HELLO_TIMEOUT_S", 0.1)
    with client.websocket_connect(url("speaker", "speaker-token")) as ws:
        expect_close(ws, 4408, "hello_timeout")


def test_invalid_frame_closes_the_speaker(client, live):
    with client.websocket_connect(url("speaker", "speaker-token")) as ws:
        ws.send_json(HELLO)
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(1, codec=CODEC_OPUS))
        expect_close(ws, 4400, "invalid_frame")
    assert get_audio_hub(client.app.state.ctx).stats(SESSION_ID)["broadcasting"] is False


def test_second_speaker_is_refused_unless_a_host_takes_over(client, live):
    with client.websocket_connect(url("speaker", "speaker-token")) as first:
        first.send_json(HELLO)
        assert first.receive_json()["type"] == "ready"
        with client.websocket_connect(url("listener", "listener-token")) as listener:
            assert listener.receive_json() == FORMAT

            with client.websocket_connect(url("speaker", "speaker2-token")) as second:
                second.send_json(HELLO)
                expect_close(second, 4423, "source_busy")
            with client.websocket_connect(url("speaker", "speaker2-token")) as second:
                second.send_json({**HELLO, "takeover": True})
                expect_close(second, 4403, "takeover_forbidden")

            with client.websocket_connect(url("speaker", "host-token")) as host:
                host.send_json({**HELLO, "takeover": True, "sample_rate": 48000})
                assert host.receive_json()["type"] == "ready"
                expect_close(first, 4410, "taken_over")
                assert listener.receive_json() == {"type": "stopped", "reason": "takeover"}
                assert listener.receive_json() == {**FORMAT, "sample_rate": 48000}
                host_frame = pack_header(1, 0, 48000, CODEC_PCM16) + PCM_20MS
                host.send_bytes(host_frame)
                assert listener.receive_bytes() == host_frame
            assert listener.receive_json() == {"type": "stopped", "reason": "source_left"}


def test_listener_limit(client, live):
    client.app.state.ctx.settings.audio_max_listeners_per_session = 1
    with client.websocket_connect(url("listener", "listener-token")):
        with client.websocket_connect(url("listener", "host-token")) as ws:
            expect_close(ws, 4429, "listener_limit")


def test_broadcast_disabled_tells_listener_and_sends_no_audio(client, live):
    live.sessions[SESSION_ID].settings = {"audio_broadcast": False}
    with client.websocket_connect(url("listener", "listener-token")) as listener:
        assert listener.receive_json() == {"type": "disabled", "reason": "audio_broadcast_off"}
        with client.websocket_connect(url("speaker", "speaker-token")) as speaker:
            speaker.send_json(HELLO)
            assert speaker.receive_json()["broadcast"] is False
            speaker.send_bytes(frame(1))
            listener.send_json({"type": "ping"})
            assert listener.receive_json() == {"type": "pong"}  # nothing (no format, no frame) came before it

            # The host turns the broadcast on mid-stream: listeners get the format and then audio.
            ctx = client.app.state.ctx
            updated = {"session": {"status": "live", "settings": {"audio_broadcast": True}}}
            client.portal.call(ctx.bus.publish, session_topic(SESSION_ID), ev.SESSION_UPDATED, updated)
            assert listener.receive_json() == FORMAT
            speaker.send_bytes(frame(2))
            assert listener.receive_bytes() == frame(2)


def test_session_ending_closes_the_speaker(client, live):
    ctx = client.app.state.ctx
    with client.websocket_connect(url("listener", "listener-token")) as listener:
        with client.websocket_connect(url("speaker", "speaker-token")) as speaker:
            speaker.send_json(HELLO)
            assert speaker.receive_json()["type"] == "ready"
            assert listener.receive_json() == FORMAT
            ended = {"session": {"status": "ended", "settings": {}}}
            client.portal.call(ctx.bus.publish, session_topic(SESSION_ID), ev.SESSION_UPDATED, ended)
            expect_close(speaker, 4409, "session_not_live")
            assert listener.receive_json() == {"type": "stopped", "reason": "source_left"}


def test_speaker_audio_is_transcribed_and_ingested(client, live, monkeypatch):
    provider = FakeProvider().install(monkeypatch)
    live.sessions[SESSION_ID].settings = {"stt_provider": "fake", "language": "en"}
    ctx = client.app.state.ctx
    with client.websocket_connect(url("speaker", "speaker-token")) as speaker:
        speaker.send_json(HELLO)
        assert speaker.receive_json()["type"] == "ready"
        for i in range(10):
            speaker.send_bytes(frame(i))
        speaker.send_json({"type": "ping"})
        assert speaker.receive_json() == {"type": "pong"}

        def transcribed() -> bool:
            return bool(provider.streams) and provider.streams[0].received == PCM_20MS * 10

        client.portal.call(_await, transcribed)
        assert provider.configs[0].keyterms == ["budget", "venue"]
        client.portal.call(provider.streams[0].emit, "next slide please")
        client.portal.call(_await, lambda: len(live.ingested) == 1)
        assert live.ingested[0].text == "next slide please"
        # The speaker leaves: the stream is finished and the provider's final result is still ingested.
        speaker.close()
        client.portal.call(_await, lambda: len(live.ingested) == 2)
    assert provider.streams[0].finished and live.ingested[-1].text == "(flushed)"
    states = [e.data["status"] for e in session_published(ctx, SESSION_ID, ev.SESSION_STT_STATE)]
    assert states == ["connecting", "active", "stopped"]


async def _await(predicate) -> None:
    from tests.test_audio_fakes import wait_until

    await wait_until(predicate)
