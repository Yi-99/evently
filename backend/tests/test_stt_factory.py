"""Provider selection: explicit names, "auto" by configured keys, browser mode, missing keys."""

from __future__ import annotations

import sys
import types

import pytest

from app.core.config import Settings
from app.transcription import factory
from app.transcription.base import TranscriptionError, TranscriptionProvider
from app.transcription.mock import MockTranscriptionProvider
from app.transcription.openai_realtime import OpenAIRealtimeProvider


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        environment="test",
        stt_provider="auto",
        deepgram_api_key=None,
        assemblyai_api_key=None,
        openai_api_key=None,
    )
    return Settings(**{**base, **overrides})


class _StandIn(TranscriptionProvider):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def open(self, config):  # pragma: no cover - never called here
        raise NotImplementedError


@pytest.fixture
def stand_in_modules(monkeypatch):
    """Fake `deepgram` / `assemblyai` modules so these tests do not depend on those files."""

    class DeepgramProvider(_StandIn):
        name = "deepgram"

    class AssemblyAIProvider(_StandIn):
        name = "assemblyai"

    dg = types.ModuleType("app.transcription.deepgram")
    dg.DeepgramProvider = DeepgramProvider
    aai = types.ModuleType("app.transcription.assemblyai")
    aai.AssemblyAIProvider = AssemblyAIProvider
    monkeypatch.setitem(sys.modules, "app.transcription.deepgram", dg)
    monkeypatch.setitem(sys.modules, "app.transcription.assemblyai", aai)
    return DeepgramProvider, AssemblyAIProvider


# -- resolve_provider_name -------------------------------------------------------


def test_auto_without_keys_is_browser():
    assert factory.resolve_provider_name(make_settings()) == "browser"
    assert factory.resolve_provider_name(make_settings(), "auto") == "browser"
    assert factory.resolve_provider_name(make_settings(), None) == "browser"


def test_auto_follows_auto_order():
    assert factory.AUTO_ORDER == ("deepgram", "assemblyai", "openai")
    everything = make_settings(deepgram_api_key="d", assemblyai_api_key="a", openai_api_key="o")
    assert factory.resolve_provider_name(everything) == "deepgram"
    assert factory.resolve_provider_name(make_settings(assemblyai_api_key="a", openai_api_key="o")) == "assemblyai"
    assert factory.resolve_provider_name(make_settings(openai_api_key="o")) == "openai"


def test_auto_defers_to_settings_default():
    assert factory.resolve_provider_name(make_settings(stt_provider="mock"), "auto") == "mock"
    assert factory.resolve_provider_name(make_settings(stt_provider="browser", openai_api_key="o")) == "browser"
    # A configured concrete provider is honoured even without its key (the error comes later).
    assert factory.resolve_provider_name(make_settings(stt_provider="deepgram")) == "deepgram"


def test_explicit_names_pass_through_and_are_normalised():
    settings = make_settings(deepgram_api_key="d")
    assert factory.resolve_provider_name(settings, "openai") == "openai"
    assert factory.resolve_provider_name(settings, " AssemblyAI ") == "assemblyai"
    assert factory.resolve_provider_name(settings, "browser") == "browser"
    assert factory.resolve_provider_name(settings, "mock") == "mock"


def test_unknown_name_is_rejected():
    with pytest.raises(ValueError, match="unknown transcription provider 'whisperx'"):
        factory.resolve_provider_name(make_settings(), "whisperx")


# -- get_transcription_provider --------------------------------------------------


def test_browser_has_no_server_side_provider():
    assert factory.get_transcription_provider(make_settings()) is None
    assert factory.get_transcription_provider(make_settings(openai_api_key="o"), "browser") is None


def test_mock_provider():
    provider = factory.get_transcription_provider(make_settings(stt_provider="mock"))
    assert isinstance(provider, MockTranscriptionProvider) and provider.name == "mock"


def test_openai_provider_gets_its_key():
    provider = factory.get_transcription_provider(make_settings(openai_api_key="sk-o"))
    assert isinstance(provider, OpenAIRealtimeProvider) and provider.name == "openai"
    assert provider._api_key == "sk-o"


def test_deepgram_and_assemblyai_are_imported_lazily(stand_in_modules):
    deepgram_cls, assemblyai_cls = stand_in_modules
    settings = make_settings(deepgram_api_key="dg", assemblyai_api_key="aai", openai_api_key="o")
    auto = factory.get_transcription_provider(settings)
    assert isinstance(auto, deepgram_cls) and auto.api_key == "dg"
    explicit = factory.get_transcription_provider(settings, "assemblyai")
    assert isinstance(explicit, assemblyai_cls) and explicit.api_key == "aai"


@pytest.mark.parametrize(
    ("name", "env_var"),
    [("deepgram", "DEEPGRAM_API_KEY"), ("assemblyai", "ASSEMBLYAI_API_KEY"), ("openai", "OPENAI_API_KEY")],
)
def test_named_provider_without_key_names_the_env_var(name, env_var):
    with pytest.raises(TranscriptionError, match=env_var):
        factory.get_transcription_provider(make_settings(), name)
    with pytest.raises(TranscriptionError, match=env_var):
        factory.get_transcription_provider(make_settings(stt_provider=name))


def test_factory_works_with_the_shared_settings_fixture(settings):
    # conftest configures `stt_provider="mock"` and no keys.
    assert factory.resolve_provider_name(settings) == "mock"
    assert isinstance(factory.get_transcription_provider(settings), MockTranscriptionProvider)
