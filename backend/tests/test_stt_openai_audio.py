"""Pure helpers of the OpenAI provider: resampler, endpointer, session config builder."""

from __future__ import annotations

import math
import struct

import pytest

from app.transcription.base import TranscriptionConfig
from app.transcription.openai_realtime import (
    Pcm16Resampler,
    _clean_keywords,
    _Endpointer,
    _language_codes,
    _session_update,
    resample_pcm16,
)


def sine(rate: int, ms: int, freq: float, amplitude: int = 10000) -> bytes:
    n = rate * ms // 1000
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))


def samples(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def zero_crossings(values: list[int]) -> int:
    return sum(1 for a, b in zip(values, values[1:], strict=False) if (a < 0) != (b < 0))


# -- resampler -----------------------------------------------------------------


def test_upsample_length_and_endpoints():
    src = struct.pack("<4h", 100, 200, 300, 400)
    out = samples(resample_pcm16(src, 16000, 24000))
    assert len(out) == 6  # 4 * 24000 / 16000
    assert out[0] == 100 and out[-1] == 400
    assert out == [100, 167, 233, 300, 367, 400]


def test_upsample_one_second_has_expected_length():
    out = resample_pcm16(sine(16000, 1000, 440), 16000, 24000)
    assert abs(len(out) // 2 - 24000) <= 1


def test_sine_frequency_is_preserved_when_upsampling():
    src = samples(sine(16000, 1000, 440))
    out = samples(resample_pcm16(sine(16000, 1000, 440), 16000, 24000))
    assert abs(zero_crossings(out) - zero_crossings(src)) <= 2  # ~880 crossings per second
    assert max(out) == pytest.approx(max(src), rel=0.02)


def test_sine_frequency_is_preserved_when_downsampling():
    src = samples(sine(48000, 1000, 300))
    out = samples(resample_pcm16(sine(48000, 1000, 300), 48000, 16000))
    assert abs(len(out) - 16000) <= 3  # the moving average eats taps-1 samples at the start
    assert abs(zero_crossings(out) - zero_crossings(src)) <= 2
    assert max(out) == pytest.approx(max(src), rel=0.05)


def test_streaming_matches_one_shot():
    signal = sine(16000, 500, 1234)
    whole = resample_pcm16(signal, 16000, 24000)
    resampler = Pcm16Resampler(16000, 24000)
    chunked = b""
    for start in range(0, len(signal), 322):  # odd-looking chunk size (161 samples)
        chunked += resampler.process(signal[start : start + 322])
    chunked += resampler.flush()
    assert chunked == whole


def test_same_rate_is_passthrough_and_half_samples_rejected():
    resampler = Pcm16Resampler(24000, 24000)
    assert resampler.process(b"\x01\x02\x03\x04") == b"\x01\x02\x03\x04"
    with pytest.raises(ValueError):
        Pcm16Resampler(16000, 24000).process(b"\x01\x02\x03")
    with pytest.raises(ValueError):
        Pcm16Resampler(0, 24000)


# -- endpointer ----------------------------------------------------------------


def feed_all(endpointer: _Endpointer, chunks: list[bytes]) -> list[int]:
    """Indices of chunks after which the endpointer asked for a commit."""
    return [i for i, chunk in enumerate(chunks) if endpointer.feed(chunk)]


def test_endpointer_commits_after_speech_then_silence():
    ep = _Endpointer(16000, silence_ms=300, max_turn_ms=10_000)
    speech = [sine(16000, 40, 200, 6000)] * 10  # 400 ms
    silence = [sine(16000, 40, 200, 0)] * 10  # 400 ms
    commits = feed_all(ep, speech + silence)
    assert commits == [10 + 7]  # 8th quiet chunk (320 ms) ends the turn


def test_endpointer_adapts_to_steady_venue_noise():
    ep = _Endpointer(16000, silence_ms=300, max_turn_ms=10**9)  # cap off: isolate the noise-floor logic
    hum = sine(16000, 40, 150, 500)  # constant hum, well above the absolute speech floor
    speech = [sine(16000, 40, 200, 8000)] * 10
    # The floor climbs slowly, so the hum counts as speech at first and one turn closes once
    # the floor has settled; after that the hum alone is quiet.
    assert len(feed_all(ep, [hum] * 1500)) <= 1  # 60 s
    assert feed_all(ep, [hum] * 100) == []
    # Speech over the hum, then back to hum = end of turn after `silence_ms`.
    assert feed_all(ep, speech + [hum] * 20) == [10 + 7]


def test_endpointer_forces_commit_at_max_turn():
    ep = _Endpointer(16000, silence_ms=300, max_turn_ms=400)
    speech = [sine(16000, 40, 200, 6000)] * 25  # 1 s without a pause
    assert feed_all(ep, speech) == [9, 19]


def test_endpointer_silence_only_commits_at_cap():
    ep = _Endpointer(16000, silence_ms=300, max_turn_ms=400)
    assert feed_all(ep, [sine(16000, 40, 200, 0)] * 10) == [9]
    assert ep.feed(b"") is False


# -- session configuration -------------------------------------------------------


def test_language_codes():
    assert _language_codes("en") == ["en"]
    assert _language_codes("en-US") == ["en"]
    assert _language_codes("pt_BR, es") == ["pt", "es"]
    assert _language_codes("zh-TW") == ["zh-tw"]
    assert _language_codes("") == [] and _language_codes("auto") == [] and _language_codes(None) == []


def test_clean_keywords():
    assert _clean_keywords(["  Evently ", "line\nbreak", "a<b", "", "Evently", "x>y"]) == ["Evently", "line break"]


def test_session_update_for_live_model():
    config = TranscriptionConfig(language="de", keyterms=["Berlin", "Tempelhof"])
    update = _session_update(config, model="gpt-live-transcribe", delay=None, prompt="A talk.", noise_reduction=None)
    assert update["type"] == "session.update"
    audio_in = update["session"]["audio"]["input"]
    assert "noise_reduction" not in audio_in
    assert audio_in["turn_detection"] is None
    assert audio_in["transcription"] == {
        "model": "gpt-live-transcribe",
        "languages": ["de"],
        "keywords": ["Berlin", "Tempelhof"],
        "prompt": "A talk.",
    }


def test_session_update_for_legacy_model_folds_keyterms_into_prompt():
    config = TranscriptionConfig(language="en", keyterms=["Evently", "next slide"])
    update = _session_update(config, model="gpt-4o-transcribe", delay=None, prompt=None, noise_reduction="far_field")
    audio_in = update["session"]["audio"]["input"]
    assert audio_in["noise_reduction"] == {"type": "far_field"}
    assert audio_in["transcription"] == {
        "model": "gpt-4o-transcribe",
        "language": "en",
        "prompt": "Evently, next slide",
    }


def test_session_update_omits_prompt_for_realtime_whisper():
    config = TranscriptionConfig(language="en", keyterms=["Evently"])
    update = _session_update(config, model="gpt-realtime-whisper", delay="minimal", prompt=None, noise_reduction=None)
    assert update["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-realtime-whisper",
        "language": "en",
        "delay": "minimal",
    }
