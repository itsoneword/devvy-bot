"""Handle voice message transcription via Mistral (Voxtral), OpenAI (Whisper),
or a local faster-whisper model (no API, runs on the server)."""

import asyncio
import io
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

import httpx
import structlog
from telegram import Voice

from src.config.settings import Settings

logger = structlog.get_logger(__name__)


@dataclass
class ProcessedVoice:
    """Result of voice message processing."""

    prompt: str
    transcription: str
    duration: int


class VoiceHandler:
    """Transcribe Telegram voice messages via Mistral, OpenAI, ElevenLabs or local whisper."""

    ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"

    def __init__(self, config: Settings):
        self.config = config
        self._mistral_client: Optional[Any] = None
        self._openai_client: Optional[Any] = None
        self._whisper_model: Optional[Any] = None
        self._elevenlabs_client: Optional[Any] = None

    def _ensure_allowed_file_size(self, file_size: Optional[int]) -> None:
        """Reject files that exceed the configured max size."""
        if (
            isinstance(file_size, int)
            and file_size > self.config.voice_max_file_size_bytes
        ):
            raise ValueError(
                "Voice message too large "
                f"({file_size / 1024 / 1024:.1f}MB). "
                f"Max allowed: {self.config.voice_max_file_size_mb}MB. "
                "Adjust VOICE_MAX_FILE_SIZE_MB if needed."
            )

    async def process_voice_message(
        self, voice: Voice, caption: Optional[str] = None
    ) -> ProcessedVoice:
        """Download and transcribe a voice message.

        1. Download .ogg bytes from Telegram
        2. Call the configured transcription API (Mistral or OpenAI)
        3. Build a prompt combining caption + transcription
        """
        initial_file_size = getattr(voice, "file_size", None)
        self._ensure_allowed_file_size(initial_file_size)

        # Resolve Telegram file metadata before downloading bytes.
        file = await voice.get_file()
        resolved_file_size = getattr(file, "file_size", None)
        self._ensure_allowed_file_size(resolved_file_size)

        # Refuse unknown-size payloads to avoid unbounded downloads.
        if not isinstance(initial_file_size, int) and not isinstance(
            resolved_file_size, int
        ):
            raise ValueError(
                "Unable to determine voice message size before download. "
                "Please retry with a smaller voice message."
            )

        # Download voice data
        voice_bytes = bytes(await file.download_as_bytearray())
        self._ensure_allowed_file_size(len(voice_bytes))

        logger.info(
            "Transcribing voice message",
            provider=self.config.voice_provider,
            duration=voice.duration,
            file_size=initial_file_size or resolved_file_size or len(voice_bytes),
        )

        if self.config.voice_provider == "openai":
            transcription = await self._transcribe_openai(voice_bytes)
        elif self.config.voice_provider == "whisper":
            transcription = await self._transcribe_whisper(voice_bytes)
        elif self.config.voice_provider == "elevenlabs":
            transcription = await self._transcribe_elevenlabs(voice_bytes)
        else:
            transcription = await self._transcribe_mistral(voice_bytes)

        logger.info(
            "Voice transcription complete",
            transcription_length=len(transcription),
            duration=voice.duration,
        )

        # Build prompt
        label = caption if caption else "Voice message transcription:"
        prompt = f"{label}\n\n{transcription}"

        dur = voice.duration
        duration_secs = int(dur.total_seconds()) if isinstance(dur, timedelta) else dur

        return ProcessedVoice(
            prompt=prompt,
            transcription=transcription,
            duration=duration_secs,
        )

    async def _transcribe_mistral(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the Mistral API (Voxtral)."""
        client = self._get_mistral_client()
        kwargs: dict = {}
        if self.config.voice_vocabulary_hints:
            kwargs["context_bias"] = self.config.voice_vocabulary_hints
        if self.config.voice_language:
            kwargs["language"] = self.config.voice_language
        try:
            response = await client.audio.transcriptions.complete_async(
                model=self.config.resolved_voice_model,
                file={
                    "content": voice_bytes,
                    "file_name": "voice.ogg",
                },
                **kwargs,
            )
        except Exception as exc:
            logger.warning(
                "Mistral transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("Mistral transcription request failed.") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("Mistral transcription returned an empty response.")
        return text

    def _get_mistral_client(self) -> Any:
        """Create and cache a Mistral client on first use."""
        if self._mistral_client is not None:
            return self._mistral_client

        try:
            from mistralai import Mistral
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'mistralai' is missing for voice transcription. "
                "Install voice extras: poetry install --extras voice"
            ) from exc

        api_key = self.config.mistral_api_key_str
        if not api_key:
            raise RuntimeError("Mistral API key is not configured.")

        self._mistral_client = Mistral(api_key=api_key)
        return self._mistral_client

    async def _transcribe_whisper(self, voice_bytes: bytes) -> str:
        """Transcribe audio locally with faster-whisper (no API).

        Model inference is CPU-bound and blocking, so it runs in a worker
        thread. faster-whisper decodes the OGG/Opus bytes in-memory via PyAV,
        and ``vad_filter`` skips non-speech so silence doesn't hallucinate.
        """
        model = self._get_whisper_model()
        hotwords = (
            ", ".join(self.config.voice_vocabulary_hints)
            if self.config.voice_vocabulary_hints
            else None
        )

        extra: dict = {}
        if self.config.voice_language:
            extra["language"] = self.config.voice_language

        def _run() -> str:
            segments, _info = model.transcribe(
                io.BytesIO(voice_bytes),
                vad_filter=True,
                hotwords=hotwords,
                **extra,
            )
            # The generator is lazy — iterating here is what runs inference.
            return "".join(segment.text for segment in segments).strip()

        try:
            text = await asyncio.to_thread(_run)
        except Exception as exc:
            logger.warning(
                "Local whisper transcription failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("Local whisper transcription failed.") from exc

        if not text:
            raise ValueError("No speech detected in the voice message.")
        return text

    def _get_whisper_model(self) -> Any:
        """Load and cache the local faster-whisper model on first use.

        Loading takes several seconds and holds the weights in memory, so the
        instance is reused across requests. Model size comes from
        ``WHISPER_MODEL`` (resolved via ``resolved_voice_model``).
        """
        if self._whisper_model is not None:
            return self._whisper_model

        try:
            from faster_whisper import WhisperModel
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'faster-whisper' is missing for local "
                "voice transcription. Install it: pip install faster-whisper"
            ) from exc

        model_size = self.config.resolved_voice_model
        logger.info("Loading local whisper model", model=model_size, device="cpu")
        self._whisper_model = WhisperModel(
            model_size, device="cpu", compute_type="int8"
        )
        return self._whisper_model

    async def _transcribe_openai(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the OpenAI Whisper API."""
        client = self._get_openai_client()
        kwargs: dict = {}
        if self.config.voice_vocabulary_hints:
            kwargs["prompt"] = ", ".join(self.config.voice_vocabulary_hints)
        if self.config.voice_language:
            kwargs["language"] = self.config.voice_language
        try:
            response = await client.audio.transcriptions.create(
                model=self.config.resolved_voice_model,
                file=("voice.ogg", voice_bytes),
                **kwargs,
            )
        except Exception as exc:
            logger.warning(
                "OpenAI transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("OpenAI transcription request failed.") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("OpenAI transcription returned an empty response.")
        return text

    async def _transcribe_elevenlabs(self, voice_bytes: bytes) -> str:
        """Transcribe audio with ElevenLabs Scribe (REST, no SDK dependency).

        ``keyterms`` is Scribe's vocabulary-bias list (max 1000 terms, <50 chars
        each) and is sent as repeated multipart fields. It carries a 20%
        surcharge on the transcription cost, so it is omitted when empty.
        """
        client = self._get_elevenlabs_client()
        # tag_audio_events=false: otherwise non-speech comes back as bracketed
        # text ("[outro jingle]") that would be forwarded as the user's message.
        data: dict = {
            "model_id": self.config.resolved_voice_model,
            "tag_audio_events": "false",
        }
        if self.config.voice_vocabulary_hints:
            data["keyterms"] = list(self.config.voice_vocabulary_hints)
        if self.config.voice_language:
            data["language_code"] = self.config.voice_language

        try:
            response = await client.post(
                "/v1/speech-to-text",
                data=data,
                files={"file": ("voice.ogg", voice_bytes, "audio/ogg")},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            # The status alone cannot tell a revoked key from an exhausted
            # quota: ElevenLabs answers 401 for both and puts the difference
            # ("quota_exceeded", "invalid_api_key", "detected_unusual_activity")
            # in the body. Logging only error_type left a 401 on 2026-09-21
            # undiagnosable from the logs. The body carries no credentials.
            detail = ""
            failed = getattr(exc, "response", None)
            if failed is not None:
                detail = (getattr(failed, "text", "") or "")[:300]
            logger.warning(
                "ElevenLabs transcription request failed",
                error_type=type(exc).__name__,
                status_code=getattr(failed, "status_code", None),
                detail=detail,
            )
            raise RuntimeError("ElevenLabs transcription request failed.") from exc

        text = ""
        if isinstance(payload, dict):
            text = (payload.get("text") or "").strip()
        if not text:
            raise ValueError("ElevenLabs transcription returned an empty response.")
        return text

    def _get_elevenlabs_client(self) -> Any:
        """Create and cache an httpx client for the ElevenLabs API on first use."""
        if self._elevenlabs_client is not None:
            return self._elevenlabs_client

        api_key = self.config.elevenlabs_api_key_str
        if not api_key:
            raise RuntimeError("ElevenLabs API key is not configured.")

        self._elevenlabs_client = httpx.AsyncClient(
            base_url=self.ELEVENLABS_BASE_URL,
            headers={"xi-api-key": api_key},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        return self._elevenlabs_client

    def _get_openai_client(self) -> Any:
        """Create and cache an OpenAI client on first use."""
        if self._openai_client is not None:
            return self._openai_client

        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'openai' is missing for voice transcription. "
                "Install voice extras: poetry install --extras voice"
            ) from exc

        api_key = self.config.openai_api_key_str
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured.")

        self._openai_client = AsyncOpenAI(api_key=api_key)
        return self._openai_client
