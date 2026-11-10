"""Transcription provider selection."""

from __future__ import annotations

from app.core.config import Settings
from app.transcription.base import TranscriptionError, TranscriptionProvider

#: Resolution order for "auto".
AUTO_ORDER = ("deepgram", "assemblyai", "openai")

#: Every name `resolve_provider_name` may return.
PROVIDER_NAMES = ("deepgram", "assemblyai", "openai", "browser", "mock")

#: Settings attribute and environment variable holding each hosted provider's key.
_KEY_FIELDS = {
    "deepgram": ("deepgram_api_key", "DEEPGRAM_API_KEY"),
    "assemblyai": ("assemblyai_api_key", "ASSEMBLYAI_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
}


def resolve_provider_name(settings: Settings, requested: str | None = None) -> str:
    """Resolve "auto"/None to a concrete name: the first of AUTO_ORDER with an API key, else "browser".

    "browser" means the client transcribes with the Web Speech API and sends text;
    there is no server-side provider object for it.

    `requested` is a per-session override. None or "auto" defers to `settings.stt_provider`;
    if that is "auto" too, the API keys decide. Explicit names are validated against
    `PROVIDER_NAMES` (ValueError otherwise) and returned unchanged.
    """
    name = (requested or "auto").strip().lower()
    if name == "auto":
        name = settings.stt_provider
    if name == "auto":
        for candidate in AUTO_ORDER:
            if getattr(settings, _KEY_FIELDS[candidate][0]):
                return candidate
        return "browser"
    if name not in PROVIDER_NAMES:
        raise ValueError(f"unknown transcription provider {name!r}; expected one of {', '.join(PROVIDER_NAMES)}")
    return name


def get_transcription_provider(settings: Settings, name: str | None = None) -> TranscriptionProvider | None:
    """Provider instance for `name` (resolved via `resolve_provider_name`); None for "browser".

    Raises `TranscriptionError` if the named provider has no API key configured.
    """
    resolved = resolve_provider_name(settings, name)
    if resolved == "browser":
        return None
    if resolved == "mock":
        from app.transcription.mock import MockTranscriptionProvider

        return MockTranscriptionProvider()

    attr, env_var = _KEY_FIELDS[resolved]
    api_key = getattr(settings, attr)
    if not api_key:
        raise TranscriptionError(f"transcription provider {resolved!r} needs {env_var} to be set")

    # Provider modules are imported lazily: each pulls in its own wire code and none is
    # needed unless selected.
    if resolved == "deepgram":
        from app.transcription.deepgram import DeepgramProvider

        return DeepgramProvider(api_key=api_key)
    if resolved == "assemblyai":
        from app.transcription.assemblyai import AssemblyAIProvider

        return AssemblyAIProvider(api_key=api_key)
    from app.transcription.openai_realtime import OpenAIRealtimeProvider

    return OpenAIRealtimeProvider(api_key=api_key)
