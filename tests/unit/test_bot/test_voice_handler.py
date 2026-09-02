"""Tests for voice handler feature."""

import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.features.voice_handler import ProcessedVoice, VoiceHandler

DEFAULT_VOCAB_HINTS = ["Claude", "Anthropic", "Sonnet", "Opus", "Haiku"]


@pytest.fixture
def mistral_config():
    """Create a mock config with Mistral settings."""
    cfg = MagicMock()
    cfg.voice_provider = "mistral"
    cfg.mistral_api_key_str = "test-api-key"
    cfg.resolved_voice_model = "voxtral-mini-latest"
    cfg.voice_max_file_size_mb = 20
    cfg.voice_max_file_size_bytes = 20 * 1024 * 1024
    cfg.voice_vocabulary_hints = list(DEFAULT_VOCAB_HINTS)
    cfg.voice_language = None
    return cfg


@pytest.fixture
def openai_config():
    """Create a mock config with OpenAI settings."""
    cfg = MagicMock()
    cfg.voice_provider = "openai"
    cfg.openai_api_key_str = "test-openai-key"
    cfg.resolved_voice_model = "whisper-1"
    cfg.voice_max_file_size_mb = 20
    cfg.voice_max_file_size_bytes = 20 * 1024 * 1024
    cfg.voice_vocabulary_hints = list(DEFAULT_VOCAB_HINTS)
    cfg.voice_language = None
    return cfg


@pytest.fixture
def whisper_config():
    """Create a mock config with local faster-whisper settings."""
    cfg = MagicMock()
    cfg.voice_provider = "whisper"
    cfg.resolved_voice_model = "small"
    cfg.voice_max_file_size_mb = 20
    cfg.voice_max_file_size_bytes = 20 * 1024 * 1024
    cfg.voice_vocabulary_hints = list(DEFAULT_VOCAB_HINTS)
    cfg.voice_language = None
    return cfg


@pytest.fixture
def voice_handler(mistral_config):
    """Create a VoiceHandler instance with Mistral config."""
    return VoiceHandler(config=mistral_config)


@pytest.fixture
def openai_voice_handler(openai_config):
    """Create a VoiceHandler instance with OpenAI config."""
    return VoiceHandler(config=openai_config)


@pytest.fixture
def whisper_voice_handler(whisper_config):
    """Create a VoiceHandler instance with local whisper config."""
    return VoiceHandler(config=whisper_config)


def _mock_voice(duration=7, file_size=1024):
    """Create a mock Telegram Voice object."""
    voice = MagicMock()
    voice.duration = duration
    voice.file_size = file_size
    mock_file = AsyncMock()
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-ogg"))
    voice.get_file = AsyncMock(return_value=mock_file)
    return voice


def test_processed_voice_dataclass():
    """ProcessedVoice stores prompt, transcription, and duration."""
    pv = ProcessedVoice(prompt="hello", transcription="world", duration=5)
    assert pv.prompt == "hello"
    assert pv.transcription == "world"
    assert pv.duration == 5


# --- Mistral provider tests ---


async def test_process_voice_message_mistral(voice_handler):
    """process_voice_message transcribes via Mistral by default."""
    voice = _mock_voice(duration=7)

    mock_response = MagicMock()
    mock_response.text = "  Hello, this is a test.  "

    mock_transcriptions = MagicMock()
    mock_transcriptions.complete_async = AsyncMock(return_value=mock_response)

    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions

    mock_client = MagicMock()
    mock_client.audio = mock_audio
    mistral_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mistralai", SimpleNamespace(Mistral=mistral_ctor))
        result = await voice_handler.process_voice_message(voice, caption=None)

    assert isinstance(result, ProcessedVoice)
    assert result.transcription == "Hello, this is a test."
    assert result.duration == 7
    assert "Voice message transcription:" in result.prompt
    assert "Hello, this is a test." in result.prompt

    mistral_ctor.assert_called_once_with(api_key="test-api-key")
    mock_transcriptions.complete_async.assert_called_once()
    call_kwargs = mock_transcriptions.complete_async.call_args
    assert call_kwargs.kwargs["model"] == "voxtral-mini-latest"
    assert call_kwargs.kwargs["context_bias"] == DEFAULT_VOCAB_HINTS


async def test_process_voice_message_with_caption(voice_handler):
    """process_voice_message uses caption as prompt label when provided."""
    voice = _mock_voice(duration=3)
    voice_handler._transcribe_mistral = AsyncMock(return_value="Transcribed text")

    result = await voice_handler.process_voice_message(
        voice, caption="Please summarize:"
    )

    assert result.prompt == "Please summarize:\n\nTranscribed text"


async def test_process_voice_message_timedelta_duration(voice_handler):
    """process_voice_message handles timedelta duration from Telegram."""
    voice = _mock_voice(duration=timedelta(seconds=15))
    voice_handler._transcribe_mistral = AsyncMock(return_value="Test")

    result = await voice_handler.process_voice_message(voice)

    assert result.duration == 15


async def test_process_voice_message_rejects_large_file(voice_handler):
    """Voice messages larger than configured limit are rejected before download."""
    voice = _mock_voice(file_size=25 * 1024 * 1024)

    with pytest.raises(ValueError, match="too large"):
        await voice_handler.process_voice_message(voice)

    voice.get_file.assert_not_awaited()


async def test_process_voice_message_rejects_large_file_from_file_metadata(
    voice_handler,
):
    """When voice.file_size is missing, Telegram file metadata is still enforced."""
    voice = MagicMock()
    voice.duration = 7
    voice.file_size = None

    telegram_file = AsyncMock()
    telegram_file.file_size = 25 * 1024 * 1024
    telegram_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-ogg"))
    voice.get_file = AsyncMock(return_value=telegram_file)

    with pytest.raises(ValueError, match="too large"):
        await voice_handler.process_voice_message(voice)

    telegram_file.download_as_bytearray.assert_not_awaited()


async def test_process_voice_message_rejects_unknown_size_before_download(
    voice_handler,
):
    """Voice messages without any size metadata are rejected before downloading."""
    voice = MagicMock()
    voice.duration = 7
    voice.file_size = None

    telegram_file = AsyncMock()
    telegram_file.file_size = None
    telegram_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-ogg"))
    voice.get_file = AsyncMock(return_value=telegram_file)

    with pytest.raises(ValueError, match="Unable to determine voice message size"):
        await voice_handler.process_voice_message(voice)

    telegram_file.download_as_bytearray.assert_not_awaited()


async def test_process_voice_message_rejects_payload_over_limit_before_api_call(
    voice_handler,
):
    """Byte payloads above limit are rejected before forwarding to provider APIs."""
    voice_handler.config.voice_max_file_size_mb = 1
    voice_handler.config.voice_max_file_size_bytes = 1 * 1024 * 1024
    voice_handler._transcribe_mistral = AsyncMock(return_value="should not be called")

    voice = _mock_voice(file_size=512 * 1024)
    voice.get_file.return_value.download_as_bytearray = AsyncMock(
        return_value=bytearray(b"x" * (2 * 1024 * 1024))
    )

    with pytest.raises(ValueError, match="too large"):
        await voice_handler.process_voice_message(voice)

    voice_handler._transcribe_mistral.assert_not_awaited()


async def test_transcribe_mistral_missing_optional_dependency(voice_handler):
    """Missing mistralai package returns a clear install hint."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mistralai", None)
        with pytest.raises(RuntimeError, match="Optional dependency 'mistralai'"):
            await voice_handler._transcribe_mistral(b"fake-ogg")


async def test_transcribe_mistral_network_error(voice_handler):
    """Network/API errors from Mistral are wrapped with provider context."""
    mock_transcriptions = MagicMock()
    mock_transcriptions.complete_async = AsyncMock(
        side_effect=Exception("network down")
    )
    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions
    mock_client = MagicMock()
    mock_client.audio = mock_audio
    mistral_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mistralai", SimpleNamespace(Mistral=mistral_ctor))
        with pytest.raises(RuntimeError, match="Mistral transcription request failed"):
            await voice_handler._transcribe_mistral(b"fake-ogg")


async def test_transcribe_mistral_reuses_cached_client(voice_handler):
    """Mistral SDK client is created once and reused across calls."""
    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_transcriptions = MagicMock()
    mock_transcriptions.complete_async = AsyncMock(return_value=mock_response)
    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions
    mock_client = MagicMock()
    mock_client.audio = mock_audio
    mistral_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mistralai", SimpleNamespace(Mistral=mistral_ctor))
        await voice_handler._transcribe_mistral(b"a")
        await voice_handler._transcribe_mistral(b"b")

    mistral_ctor.assert_called_once_with(api_key="test-api-key")
    assert mock_transcriptions.complete_async.await_count == 2


async def test_transcribe_mistral_error_message_does_not_echo_exception_details(
    voice_handler,
):
    """Provider exception details are not surfaced in user-facing error text."""
    leaked_secret = "sk-super-secret-token"

    mock_transcriptions = MagicMock()
    mock_transcriptions.complete_async = AsyncMock(
        side_effect=Exception(f"Authorization: Bearer {leaked_secret}")
    )
    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions
    mock_client = MagicMock()
    mock_client.audio = mock_audio
    mistral_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mistralai", SimpleNamespace(Mistral=mistral_ctor))
        with pytest.raises(RuntimeError) as exc_info:
            await voice_handler._transcribe_mistral(b"fake-ogg")

    assert str(exc_info.value) == "Mistral transcription request failed."
    assert leaked_secret not in str(exc_info.value)


async def test_transcribe_mistral_omits_context_bias_when_hints_empty(voice_handler):
    """No context_bias kwarg is sent when the vocabulary hint list is empty."""
    voice_handler.config.voice_vocabulary_hints = []

    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_transcriptions = MagicMock()
    mock_transcriptions.complete_async = AsyncMock(return_value=mock_response)
    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions
    mock_client = MagicMock()
    mock_client.audio = mock_audio
    mistral_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mistralai", SimpleNamespace(Mistral=mistral_ctor))
        await voice_handler._transcribe_mistral(b"fake-ogg")

    call_kwargs = mock_transcriptions.complete_async.call_args
    assert "context_bias" not in call_kwargs.kwargs


# --- OpenAI provider tests ---


async def test_process_voice_message_openai(openai_voice_handler):
    """process_voice_message transcribes via OpenAI Whisper."""
    voice = _mock_voice(duration=10)

    mock_response = MagicMock()
    mock_response.text = "  Hello from Whisper.  "

    mock_transcriptions = MagicMock()
    mock_transcriptions.create = AsyncMock(return_value=mock_response)

    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions

    mock_client = MagicMock()
    mock_client.audio = mock_audio
    openai_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=openai_ctor))
        result = await openai_voice_handler.process_voice_message(voice, caption=None)

    assert isinstance(result, ProcessedVoice)
    assert result.transcription == "Hello from Whisper."
    assert result.duration == 10
    assert "Voice message transcription:" in result.prompt

    openai_ctor.assert_called_once_with(api_key="test-openai-key")
    mock_transcriptions.create.assert_called_once()
    call_kwargs = mock_transcriptions.create.call_args
    assert call_kwargs.kwargs["model"] == "whisper-1"
    assert call_kwargs.kwargs["file"] == ("voice.ogg", b"fake-ogg")
    assert call_kwargs.kwargs["prompt"] == ", ".join(DEFAULT_VOCAB_HINTS)


async def test_process_voice_message_openai_with_caption(openai_voice_handler):
    """OpenAI provider uses caption as prompt label when provided."""
    voice = _mock_voice(duration=5)
    openai_voice_handler._transcribe_openai = AsyncMock(
        return_value="Whisper transcription"
    )

    result = await openai_voice_handler.process_voice_message(
        voice, caption="Translate this:"
    )

    assert result.prompt == "Translate this:\n\nWhisper transcription"


async def test_transcribe_openai_empty_response(openai_voice_handler):
    """OpenAI empty transcriptions are rejected."""
    mock_response = MagicMock()
    mock_response.text = "   "

    mock_transcriptions = MagicMock()
    mock_transcriptions.create = AsyncMock(return_value=mock_response)

    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions

    mock_client = MagicMock()
    mock_client.audio = mock_audio
    openai_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=openai_ctor))
        with pytest.raises(ValueError, match="empty response"):
            await openai_voice_handler._transcribe_openai(b"fake-ogg")


async def test_transcribe_openai_reuses_cached_client(openai_voice_handler):
    """OpenAI SDK client is created once and reused across calls."""
    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_transcriptions = MagicMock()
    mock_transcriptions.create = AsyncMock(return_value=mock_response)
    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions
    mock_client = MagicMock()
    mock_client.audio = mock_audio
    openai_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=openai_ctor))
        await openai_voice_handler._transcribe_openai(b"a")
        await openai_voice_handler._transcribe_openai(b"b")

    openai_ctor.assert_called_once_with(api_key="test-openai-key")
    assert mock_transcriptions.create.await_count == 2


async def test_transcribe_openai_omits_prompt_when_hints_empty(openai_voice_handler):
    """No prompt kwarg is sent when the vocabulary hint list is empty."""
    openai_voice_handler.config.voice_vocabulary_hints = []

    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_transcriptions = MagicMock()
    mock_transcriptions.create = AsyncMock(return_value=mock_response)
    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions
    mock_client = MagicMock()
    mock_client.audio = mock_audio
    openai_ctor = MagicMock(return_value=mock_client)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=openai_ctor))
        await openai_voice_handler._transcribe_openai(b"fake-ogg")

    call_kwargs = mock_transcriptions.create.call_args
    assert "prompt" not in call_kwargs.kwargs


# --- Local whisper (faster-whisper) provider tests ---


def _mock_whisper_model(text="Hello from local whisper."):
    """Create a mock faster-whisper WhisperModel with one segment."""
    segment = SimpleNamespace(text=text)
    model = MagicMock()
    model.transcribe = MagicMock(return_value=([segment], SimpleNamespace()))
    return model


async def test_process_voice_message_whisper(whisper_voice_handler):
    """process_voice_message transcribes via local faster-whisper."""
    voice = _mock_voice(duration=4)
    mock_model = _mock_whisper_model(" Hello from local whisper. ")
    whisper_ctor = MagicMock(return_value=mock_model)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper_ctor)
        )
        result = await whisper_voice_handler.process_voice_message(voice, caption=None)

    assert isinstance(result, ProcessedVoice)
    assert result.transcription == "Hello from local whisper."
    assert result.duration == 4

    whisper_ctor.assert_called_once_with("small", device="cpu", compute_type="int8")
    mock_model.transcribe.assert_called_once()
    call_kwargs = mock_model.transcribe.call_args
    assert call_kwargs.kwargs["vad_filter"] is True
    assert call_kwargs.kwargs["hotwords"] == ", ".join(DEFAULT_VOCAB_HINTS)


async def test_transcribe_whisper_hotwords_none_when_hints_empty(whisper_voice_handler):
    """hotwords is passed as None when the vocabulary hint list is empty."""
    whisper_voice_handler.config.voice_vocabulary_hints = []
    mock_model = _mock_whisper_model()
    whisper_ctor = MagicMock(return_value=mock_model)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper_ctor)
        )
        await whisper_voice_handler._transcribe_whisper(b"fake-ogg")

    call_kwargs = mock_model.transcribe.call_args
    assert call_kwargs.kwargs["hotwords"] is None


async def test_transcribe_whisper_missing_optional_dependency(whisper_voice_handler):
    """Missing faster-whisper package returns a clear install hint."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "faster_whisper", None)
        with pytest.raises(RuntimeError, match="Optional dependency 'faster-whisper'"):
            await whisper_voice_handler._transcribe_whisper(b"fake-ogg")


async def test_transcribe_whisper_empty_response(whisper_voice_handler):
    """No speech detected raises a clear error."""
    mock_model = _mock_whisper_model(text="   ")
    whisper_ctor = MagicMock(return_value=mock_model)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper_ctor)
        )
        with pytest.raises(ValueError, match="No speech detected"):
            await whisper_voice_handler._transcribe_whisper(b"fake-ogg")


async def test_transcribe_whisper_reuses_cached_model(whisper_voice_handler):
    """faster-whisper model is loaded once and reused across calls."""
    mock_model = _mock_whisper_model()
    whisper_ctor = MagicMock(return_value=mock_model)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper_ctor)
        )
        await whisper_voice_handler._transcribe_whisper(b"a")
        await whisper_voice_handler._transcribe_whisper(b"b")

    whisper_ctor.assert_called_once_with("small", device="cpu", compute_type="int8")
    assert mock_model.transcribe.call_count == 2


# --------------------------------------------------------------------------
# ElevenLabs Scribe provider


@pytest.fixture
def elevenlabs_config():
    """Create a mock config with ElevenLabs settings."""
    cfg = MagicMock()
    cfg.voice_provider = "elevenlabs"
    cfg.elevenlabs_api_key_str = "test-xi-key"
    cfg.resolved_voice_model = "scribe_v2"
    cfg.voice_max_file_size_mb = 20
    cfg.voice_max_file_size_bytes = 20 * 1024 * 1024
    cfg.voice_vocabulary_hints = list(DEFAULT_VOCAB_HINTS)
    cfg.voice_language = None
    return cfg


@pytest.fixture
def elevenlabs_voice_handler(elevenlabs_config):
    """Create a VoiceHandler instance with ElevenLabs config."""
    return VoiceHandler(config=elevenlabs_config)


def _mock_elevenlabs_client(payload=None, *, raise_exc=None):
    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=raise_exc)
    response.json = MagicMock(return_value=payload if payload is not None else {})
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    return client


async def test_process_voice_message_elevenlabs(elevenlabs_voice_handler):
    """ElevenLabs path posts multipart with model_id + keyterms and returns the text."""
    client = _mock_elevenlabs_client({"text": "Ask Claude about Sonnet."})
    elevenlabs_voice_handler._elevenlabs_client = client

    voice = MagicMock()
    voice.file_id = "voice123"
    voice.duration = 4
    voice.file_size = 1024
    file_obj = MagicMock()
    file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-ogg"))
    voice.get_file = AsyncMock(return_value=file_obj)

    result = await elevenlabs_voice_handler.process_voice_message(voice, None)

    assert isinstance(result, ProcessedVoice)
    assert result.transcription == "Ask Claude about Sonnet."
    call = client.post.call_args
    assert call.args[0] == "/v1/speech-to-text"
    assert call.kwargs["data"]["model_id"] == "scribe_v2"
    assert call.kwargs["data"]["keyterms"] == DEFAULT_VOCAB_HINTS
    assert call.kwargs["data"]["tag_audio_events"] == "false"
    assert "language_code" not in call.kwargs["data"]
    assert call.kwargs["files"]["file"][0] == "voice.ogg"
    assert call.kwargs["files"]["file"][1] == b"fake-ogg"


async def test_transcribe_elevenlabs_omits_keyterms_when_hints_empty(
    elevenlabs_voice_handler,
):
    """No keyterms field is sent when the hint list is empty (it is surcharged)."""
    elevenlabs_voice_handler.config.voice_vocabulary_hints = []
    client = _mock_elevenlabs_client({"text": "ok"})
    elevenlabs_voice_handler._elevenlabs_client = client

    await elevenlabs_voice_handler._transcribe_elevenlabs(b"fake-ogg")

    assert "keyterms" not in client.post.call_args.kwargs["data"]


async def test_transcribe_elevenlabs_passes_language_code(elevenlabs_voice_handler):
    """voice_language is forwarded as ElevenLabs' language_code field."""
    elevenlabs_voice_handler.config.voice_language = "en"
    client = _mock_elevenlabs_client({"text": "ok"})
    elevenlabs_voice_handler._elevenlabs_client = client

    await elevenlabs_voice_handler._transcribe_elevenlabs(b"fake-ogg")

    assert client.post.call_args.kwargs["data"]["language_code"] == "en"


async def test_transcribe_elevenlabs_empty_response(elevenlabs_voice_handler):
    """An empty text field is treated as a transcription failure."""
    elevenlabs_voice_handler._elevenlabs_client = _mock_elevenlabs_client({"text": ""})

    with pytest.raises(ValueError, match="empty response"):
        await elevenlabs_voice_handler._transcribe_elevenlabs(b"fake-ogg")


async def test_transcribe_elevenlabs_http_error_does_not_echo_details(
    elevenlabs_voice_handler,
):
    """HTTP failures surface as a fixed message that never leaks the API response."""
    leaked = "xi-api-key test-xi-key"
    elevenlabs_voice_handler._elevenlabs_client = _mock_elevenlabs_client(
        raise_exc=Exception(leaked)
    )

    with pytest.raises(RuntimeError) as exc_info:
        await elevenlabs_voice_handler._transcribe_elevenlabs(b"fake-ogg")

    assert str(exc_info.value) == "ElevenLabs transcription request failed."
    assert leaked not in str(exc_info.value)


def test_get_elevenlabs_client_requires_api_key(elevenlabs_voice_handler):
    """Client creation fails fast when no key is configured."""
    elevenlabs_voice_handler.config.elevenlabs_api_key_str = None

    with pytest.raises(RuntimeError, match="API key is not configured"):
        elevenlabs_voice_handler._get_elevenlabs_client()


def test_get_elevenlabs_client_sets_auth_header_and_is_cached(
    elevenlabs_voice_handler,
):
    """The httpx client carries the xi-api-key header and is reused."""
    client = elevenlabs_voice_handler._get_elevenlabs_client()

    assert client.headers["xi-api-key"] == "test-xi-key"
    assert str(client.base_url).startswith("https://api.elevenlabs.io")
    assert elevenlabs_voice_handler._get_elevenlabs_client() is client


# --------------------------------------------------------------------------
# voice_language passthrough on the other providers


async def test_transcribe_mistral_passes_language(voice_handler):
    """voice_language is forwarded as Mistral's language kwarg."""
    voice_handler.config.voice_language = "en"
    mock_response = MagicMock()
    mock_response.text = "ok"
    mock_transcriptions = MagicMock()
    mock_transcriptions.complete_async = AsyncMock(return_value=mock_response)
    mock_client = MagicMock()
    mock_client.audio = MagicMock(transcriptions=mock_transcriptions)
    voice_handler._mistral_client = mock_client

    await voice_handler._transcribe_mistral(b"fake-ogg")

    assert mock_transcriptions.complete_async.call_args.kwargs["language"] == "en"


async def test_transcribe_whisper_passes_language(whisper_config):
    """voice_language is forwarded to faster-whisper's language kwarg."""
    whisper_config.voice_language = "en"
    handler = VoiceHandler(config=whisper_config)
    model = MagicMock()
    model.transcribe = MagicMock(
        return_value=([SimpleNamespace(text="ok")], SimpleNamespace())
    )
    handler._whisper_model = model

    await handler._transcribe_whisper(b"fake-ogg")

    assert model.transcribe.call_args.kwargs["language"] == "en"
