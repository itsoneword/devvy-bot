"""``/voice`` — pick the speech-to-text provider, check it, switch live (dv-6b6c).

Step 0 of onboarding: voice must work before the questionnaire, so this is a
standalone entry point the onboarding gate calls (``voice_command``).

Flow: buttons ``vset:<provider>`` -> a cloud provider without a key gets the
exact ``/setkey`` line to send (keys enter only through ``/setkey``, dv-0202;
no second key-entry path here) -> ``/setkey`` hands the key back via
:func:`takes_voice_key` / :func:`apply_voice_key` -> one tiny transcription
(``VoiceHandler.probe``) -> only if it passes: persist ``VOICE_PROVIDER`` /
``ENABLE_VOICE_MESSAGES`` to the config ``.env``, mutate the shared Settings
and swap the handler in the live FeatureRegistry. No restart. A rejected key
leaves the previous provider running untouched.

The bot replies in text only; there is no speech synthesis (owner, 2026-10-11).
"""

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ....config.env_file import write_env
from ....config.voice import (
    VOICE_KEY_NAMES,
    VOICE_PROVIDERS,
    voice_api_key,
    voice_provider_ready,
)
from ....security.audit import AuditLogger
from ....security.keystore import KeyStore, mask
from ...features.voice_handler import VoiceHandler
from ...utils.html_format import escape_html

logger = structlog.get_logger()

# Cloud provider the owner picked and whose key /setkey should pick up.
PENDING_KEY = "voice_setup_pending"

_LABELS: Dict[str, str] = {
    "elevenlabs": "ElevenLabs",
    "openai": "OpenAI",
    "mistral": "Mistral",
    "whisper": "Local Whisper",
}


class VoiceSetupError(Exception):
    """A provider failed its check. Message is safe to show (no key in it)."""


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ElevenLabs", callback_data="vset:elevenlabs"),
                InlineKeyboardButton("OpenAI", callback_data="vset:openai"),
                InlineKeyboardButton("Mistral", callback_data="vset:mistral"),
            ],
            [
                InlineKeyboardButton(
                    "💻 Local Whisper (на сервере)", callback_data="vset:whisper"
                )
            ],
            [InlineKeyboardButton("⌨️ Только текст", callback_data="vset:off")],
        ]
    )


def _keystore(context: ContextTypes.DEFAULT_TYPE) -> Optional[KeyStore]:
    from .keys import _store

    return _store(context)


def _current(context: ContextTypes.DEFAULT_TYPE) -> str:
    settings = context.bot_data["settings"]
    if not settings.enable_voice_messages:
        return "выключен (только текст)"
    name = str(settings.voice_provider_display_name)
    if not voice_provider_ready(settings, keystore=_keystore(context)):
        return f"{name} — нет ключа, не работает"
    return name


def _env_file(context: ContextTypes.DEFAULT_TYPE) -> Path:
    """The config .env ``load_config`` reads: ``--config-file`` or ./.env."""
    return Path(context.bot_data.get("env_file") or ".env")


async def _audit(
    context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int], args: list, ok: bool
) -> None:
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger and user_id is not None:
        await audit_logger.log_command(user_id, "voice", args, ok)


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask how to talk to the bot; show what is live now."""
    await update.message.reply_text(
        "🎙 <b>Хочешь общаться голосом?</b>\n"
        "Голосовые сообщения распознаются в текст; бот отвечает текстом.\n\n"
        f"Сейчас: {escape_html(_current(context))}\n\n"
        "• ElevenLabs / OpenAI / Mistral — облако, нужен API-ключ\n"
        "• Local Whisper — на этом сервере, без ключа; ~0.5 GB RAM, "
        "первый запуск скачивает модель",
        parse_mode="HTML",
        reply_markup=_keyboard(),
    )
    await _audit(context, update.effective_user.id, [], True)


def _reason(exc: BaseException) -> str:
    """Short, key-free description of why a probe failed."""
    cause = exc.__cause__ or exc
    response = getattr(cause, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"провайдер ответил HTTP {status}"
    text = str(exc) or type(exc).__name__
    return text[:200]


def _persist(context: ContextTypes.DEFAULT_TYPE, values: Dict[str, str]) -> None:
    # No .bak copy: this runs on every switch and would litter the bot dir.
    write_env(_env_file(context), values, backup=False)


async def switch_voice(
    context: ContextTypes.DEFAULT_TYPE, provider: Optional[str]
) -> None:
    """Make ``provider`` (None = text only) the live STT, or raise.

    The candidate is probed on a copy of Settings; the live handler and
    Settings are touched only after the probe and the .env write succeed.
    """
    settings = context.bot_data["settings"]
    features = context.bot_data.get("features")
    old = features.get_voice_handler() if features is not None else None

    if provider is None:
        _persist(context, {"ENABLE_VOICE_MESSAGES": "false"})
        settings.enable_voice_messages = False
        if features is not None:
            features.set_voice_handler(None)
        if old is not None:
            await old.aclose()
        logger.info("Voice switched off")
        return

    key = voice_api_key(settings, provider, _keystore(context))
    if provider != "whisper" and not key:
        raise VoiceSetupError("нет ключа")
    candidate = settings.model_copy(
        update={"voice_provider": provider, "enable_voice_messages": True}
    )
    handler = VoiceHandler(config=candidate, api_key=key)
    try:
        await handler.probe()
    except Exception as exc:
        await handler.aclose()
        logger.warning(
            "Voice provider check failed",
            provider=provider,
            error_type=type(exc).__name__,
        )
        raise VoiceSetupError(_reason(exc)) from exc

    _persist(context, {"VOICE_PROVIDER": provider, "ENABLE_VOICE_MESSAGES": "true"})
    settings.voice_provider = provider
    settings.enable_voice_messages = True
    handler.config = settings  # keep the warm client/model, follow live Settings
    if features is not None:
        features.set_voice_handler(handler)
    if old is not None and old is not handler:
        await old.aclose()
    logger.info("Voice provider switched", provider=provider)


def _setkey_line(provider: str) -> str:
    return f"<code>/setkey {provider} {VOICE_KEY_NAMES[provider]} &lt;ключ&gt;</code>"


async def handle_voice_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    """Handle ``vset:*`` buttons (owner-gated by the dispatcher)."""
    choice = data.split(":", 1)[1]
    user_id = getattr(query.from_user, "id", None)
    if choice == "off":
        context.bot_data.pop(PENDING_KEY, None)
        await switch_voice(context, None)
        await query.edit_message_text(
            "⌨️ Голос выключен — только текст. Включить снова: /voice"
        )
        await _audit(context, user_id, ["off"], True)
        return
    if choice not in VOICE_PROVIDERS:
        await query.edit_message_text("Неизвестный вариант.")
        return

    label = _LABELS[choice]
    settings = context.bot_data["settings"]
    if choice != "whisper" and not voice_api_key(settings, choice, _keystore(context)):
        context.bot_data[PENDING_KEY] = choice
        await query.edit_message_text(
            f"🔑 Пришли ключ {label} одной командой:\n{_setkey_line(choice)}\n\n"
            "Сообщение с ключом удалится сразу, модель его не увидит. "
            "Проверю ключ одним коротким запросом и только потом переключу.",
            parse_mode="HTML",
        )
        return

    context.bot_data.pop(PENDING_KEY, None)
    await query.edit_message_text(
        f"⏳ Проверяю {label}…"
        + (
            "\nПервый запуск скачивает модель (до ~0.5 GB), это может занять "
            "несколько минут."
            if choice == "whisper"
            else ""
        )
    )
    try:
        await switch_voice(context, choice)
    except VoiceSetupError as exc:
        extra = ""
        if choice != "whisper":
            context.bot_data[PENDING_KEY] = choice
            extra = f"\nДругой ключ: {_setkey_line(choice)}"
        await query.edit_message_text(
            f"❌ {escape_html(label)} не работает: {escape_html(str(exc))}.\n"
            f"Голос по-прежнему: {escape_html(_current(context))}{extra}",
            parse_mode="HTML",
        )
        await _audit(context, user_id, [choice], False)
        return
    await query.edit_message_text(
        f"✅ Голос: {escape_html(settings.voice_provider_display_name)}. "
        "Пришли голосовое — распознаю в текст.",
        parse_mode="HTML",
    )
    await _audit(context, user_id, [choice], True)


def takes_voice_key(
    context: ContextTypes.DEFAULT_TYPE, service: str, name: str
) -> bool:
    """Should this ``/setkey`` drive a voice switch?

    Only for the voice key of the provider the owner picked in /voice, or of
    the live provider (a key rotation). Storing, say, an OpenAI key while
    ElevenLabs is live must not silently switch.
    """
    if VOICE_KEY_NAMES.get(service) != name:
        return False
    if context.bot_data.get(PENDING_KEY) == service:
        return True
    settings = context.bot_data.get("settings")
    return bool(
        settings is not None
        and settings.enable_voice_messages
        and settings.voice_provider == service
    )


async def apply_voice_key(
    context: ContextTypes.DEFAULT_TYPE,
    reply: Callable[[str], Awaitable[None]],
    store: KeyStore,
    service: str,
    name: str,
    value: str,
    previous: Optional[str],
    note: str = "",
) -> bool:
    """Check a just-stored voice key and switch to it; undo the store if bad."""
    label = _LABELS[service]
    try:
        await switch_voice(context, service)
    except VoiceSetupError as exc:
        # A rejected key must not win at the next boot: put back what was there.
        if previous:
            store.set(service, name, previous)
        else:
            store.delete(service, name)
        await reply(
            f"❌ Ключ отклонён ({escape_html(label)}): {escape_html(str(exc))}. "
            "Ключ не сохранён.\n"
            f"Голос по-прежнему: {escape_html(_current(context))}\n"
            f"Попробовать другой: {_setkey_line(service)}" + note
        )
        return False
    context.bot_data.pop(PENDING_KEY, None)
    await reply(
        f"✅ Ключ принят (<code>{escape_html(mask(value.strip()))}</code>). "
        f"Голос: {escape_html(context.bot_data['settings'].voice_provider_display_name)}"
        " — пришли голосовое." + note
    )
    return True
