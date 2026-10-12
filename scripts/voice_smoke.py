#!/usr/bin/env python3
"""End-to-end voice check: a real audio file -> transcript, one provider (dv-6b6c).

Runs the bot's own path (``VoiceHandler.transcribe_bytes`` / ``probe`` with the
key from ``src.config.voice``), not a re-implementation.

Usage (repo root, like the service's WorkingDirectory):
    poetry run python scripts/voice_smoke.py note.ogg --provider whisper
    poetry run python scripts/voice_smoke.py --probe --provider whisper
    scripts/withkeys elevenlabs -- poetry run python scripts/voice_smoke.py \\
        note.ogg --provider elevenlabs

Prints provider, model, latency and transcript length; the transcript itself
only with ``--show`` (voice notes are private). Exit 1 on failure.
"""

import argparse
import asyncio
import contextlib
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot.features.voice_handler import VoiceHandler  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.config.voice import (  # noqa: E402
    VOICE_PROVIDERS,
    boot_keystore,
    voice_api_key,
)


def _settings() -> Settings:
    """The bot's config when ./.env is there, else env-only with dummy bot ids."""
    if Path(".env").exists():
        from src.config.loader import load_config

        with contextlib.redirect_stdout(io.StringIO()):
            return load_config()
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="smoke:token", telegram_bot_username="smoke_bot"
    )


async def _run(args: argparse.Namespace) -> int:
    settings = _settings()
    provider = args.provider or settings.voice_provider
    update = {"voice_provider": provider, "enable_voice_messages": True}
    if args.model:
        update["voice_transcription_model"] = args.model
    if provider == "whisper" and args.whisper_model:
        update["whisper_model"] = args.whisper_model
    settings = settings.model_copy(update=update)
    key = voice_api_key(settings, provider, boot_keystore())
    if provider != "whisper" and not key:
        print(f"FAIL {provider}: no API key (key store or secrets file)")
        return 1
    handler = VoiceHandler(config=settings, api_key=key)
    started = time.monotonic()
    try:
        if args.probe:
            await handler.probe()
            print(
                f"OK probe {provider} model={settings.resolved_voice_model} "
                f"{time.monotonic() - started:.1f}s"
            )
            return 0
        text = await handler.transcribe_bytes(Path(args.audio).read_bytes())
    except Exception as exc:
        cause = exc.__cause__ or exc
        status = getattr(getattr(cause, "response", None), "status_code", None)
        print(f"FAIL {provider}: {type(exc).__name__}: {exc} (HTTP {status})")
        return 1
    finally:
        await handler.aclose()
    print(
        f"OK {provider} model={settings.resolved_voice_model} "
        f"{time.monotonic() - started:.1f}s chars={len(text)}"
    )
    if args.show:
        print(text)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", nargs="?", help="audio file (e.g. a Telegram .ogg)")
    parser.add_argument("--provider", choices=VOICE_PROVIDERS)
    parser.add_argument("--probe", action="store_true", help="key/model check only")
    parser.add_argument("--model", help="override VOICE_TRANSCRIPTION_MODEL")
    parser.add_argument("--whisper-model", help="override WHISPER_MODEL")
    parser.add_argument("--show", action="store_true", help="print the transcript")
    args = parser.parse_args()
    if not args.probe and not args.audio:
        parser.error("audio file required (or --probe)")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
