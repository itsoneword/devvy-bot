"""dv-6b6c — the voice key resolver, the shared .env writer, and the /voice
edges the scenario suite (test_voice_setup.py) does not pin: key rotation,
guest buttons, and the FeatureFlags gate reading the key store."""

from unittest.mock import AsyncMock, MagicMock

import httpx

from src.config import create_test_config
from src.config.env_file import upsert_env, write_env
from src.config.features import FeatureFlags
from src.config.voice import voice_api_key, voice_available, voice_provider_ready
from src.security.keystore import KeyStore

from .test_voice_setup import (  # noqa: F401  (fixtures)
    BAD_KEY,
    GOOD_KEY,
    OWNER,
    WHISPER_TEXT,
    Bot,
    _env_values,
    env,
    fake_elevenlabs,
    fake_whisper,
)


def _settings(tmp_path, **kw):
    values = dict(approved_directory=str(tmp_path), enable_voice_messages=True)
    values.update(kw)
    return create_test_config(**values)


# --- resolver ---------------------------------------------------------------


def test_keystore_key_wins_over_settings(tmp_path):
    store = KeyStore(tmp_path / "tools")
    store.set("elevenlabs", "ELEVENLABS_API_KEY", "from-store-123456")
    s = _settings(tmp_path, voice_provider="elevenlabs", elevenlabs_api_key="from-env")
    assert voice_api_key(s, keystore=store) == "from-store-123456"
    assert voice_api_key(s) == "from-env"


def test_settings_key_is_the_fallback_and_whisper_is_keyless(tmp_path):
    store = KeyStore(tmp_path / "tools")
    s = _settings(tmp_path, voice_provider="openai", openai_api_key="sk-env")
    assert voice_api_key(s, keystore=store) == "sk-env"
    assert voice_api_key(s, "mistral", store) is None
    assert voice_provider_ready(s, "whisper", store) is True
    assert voice_provider_ready(s, "mistral", store) is False


def test_voice_off_is_unavailable_even_with_a_key(tmp_path):
    s = _settings(
        tmp_path,
        voice_provider="elevenlabs",
        elevenlabs_api_key="xi",
        enable_voice_messages=False,
    )
    assert voice_available(s) is False


def test_feature_flag_sees_a_keystore_only_key(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(root))
    s = _settings(tmp_path, voice_provider="elevenlabs", elevenlabs_api_key=None)
    assert FeatureFlags(s).voice_messages_enabled is False
    KeyStore(root / "secrets" / "tools").set(
        "elevenlabs", "ELEVENLABS_API_KEY", "xi-store-123456"
    )
    assert FeatureFlags(s).voice_messages_enabled is True


# --- .env writer --------------------------------------------------------------


def test_write_env_without_backup_keeps_other_lines(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("A=1\n# VOICE_PROVIDER=mistral\nVOICE_PROVIDER=openai\nB=2\n")
    saved = write_env(path, {"VOICE_PROVIDER": "whisper", "NEW": "x"}, backup=False)
    assert saved == path
    assert list(tmp_path.iterdir()) == [path]  # no .bak, no temp file left
    text = path.read_text()
    assert "A=1" in text and "B=2" in text and "# VOICE_PROVIDER=mistral" in text
    assert "VOICE_PROVIDER=whisper" in text and "VOICE_PROVIDER=openai" not in text
    assert text.endswith("NEW=x\n")
    assert path.stat().st_mode & 0o777 == 0o600


def test_capture_owner_uses_the_shared_writer():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "capture_owner.py"
    spec = importlib.util.spec_from_file_location("_capture_owner_t", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.upsert_env("X=1\n", "X", "2") == upsert_env("X=1\n", "X", "2")
    assert module.write_env.__code__.co_filename == write_env.__code__.co_filename


# --- /voice edges -------------------------------------------------------------


async def test_rotating_live_key_to_a_bad_one_keeps_the_old_key(
    env, fake_whisper, fake_elevenlabs  # noqa: F811
):
    """Live provider ElevenLabs: a rejected new key is not kept in the store,
    so the next boot still uses the good one."""
    store = KeyStore(env.tmp / "secrets" / "tools")
    store.set("elevenlabs", "ELEVENLABS_API_KEY", GOOD_KEY)
    bot = Bot(env, keystore=store, voice_provider="elevenlabs")
    bot.features.keystore = store
    bot.features.set_voice_handler(None)
    await bot.press("vset:elevenlabs")  # key already stored -> probe + switch
    assert bot.voice_handler() is not None
    live = bot.voice_handler()

    await bot.command("setkey", f"/setkey elevenlabs ELEVENLABS_API_KEY {BAD_KEY}")

    assert "ключ отклонён" in bot.all_text().lower()
    assert store.env("elevenlabs") == {"ELEVENLABS_API_KEY": GOOD_KEY}
    assert bot.voice_handler() is live
    assert BAD_KEY not in bot.all_text()


async def test_good_key_rotation_switches_without_pending(
    env, fake_whisper, fake_elevenlabs  # noqa: F811
):
    store = KeyStore(env.tmp / "secrets" / "tools")
    store.set("elevenlabs", "ELEVENLABS_API_KEY", BAD_KEY)
    bot = Bot(env, keystore=store, voice_provider="elevenlabs")
    await bot.command("setkey", f"/setkey elevenlabs ELEVENLABS_API_KEY {GOOD_KEY}")
    assert "ключ принят" in bot.all_text().lower()
    assert _env_values(env.env_file).get("VOICE_PROVIDER") == "elevenlabs"
    assert store.env("elevenlabs") == {"ELEVENLABS_API_KEY": GOOD_KEY}


async def test_guest_cannot_press_voice_buttons(env, fake_whisper):  # noqa: F811
    bot = Bot(env, voice_provider="whisper")
    ctx = bot.context()
    ctx.user_data = {"role": "guest"}
    query = MagicMock()
    query.data = "vset:off"
    query.answer = AsyncMock()
    query.from_user.id = OWNER + 1
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    await bot.orchestrator._agentic_callback(update, ctx)

    assert bot.settings.enable_voice_messages is True
    assert bot.voice_handler() is not None
    assert "ENABLE_VOICE_MESSAGES" not in _env_values(env.env_file)


async def test_failed_whisper_probe_keeps_previous_provider(
    env, monkeypatch  # noqa: F811
):
    import faster_whisper

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(faster_whisper, "WhisperModel", broken)
    bot = Bot(env, voice_provider="elevenlabs", elevenlabs_api_key=None)
    await bot.press("vset:whisper")
    assert bot.settings.voice_provider == "elevenlabs"
    assert bot.voice_handler() is None
    assert "не работает" in bot.all_text()
    assert "VOICE_PROVIDER" not in _env_values(env.env_file)


async def test_voice_note_still_transcribes_after_switch_to_whisper(
    env, fake_whisper  # noqa: F811
):
    bot = Bot(env, voice_provider="whisper")
    await bot.press("vset:off")
    await bot.press("vset:whisper")
    prompt = await bot.send_voice()
    assert prompt is not None and WHISPER_TEXT in prompt


# --- probe ------------------------------------------------------------------

_REAL_ASYNC_CLIENT = httpx.AsyncClient


async def _probe_with_status(tmp_path, monkeypatch, status):
    from src.bot.features.voice_handler import VoiceHandler

    class _Client(_REAL_ASYNC_CLIENT):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(
                lambda r: httpx.Response(status, json={"detail": "x"})
            )
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    s = _settings(tmp_path, voice_provider="elevenlabs")
    handler = VoiceHandler(config=s, api_key="xi-some-key-123456")
    try:
        await handler.probe()
    finally:
        await handler.aclose()


async def test_probe_accepts_a_refused_clip_but_not_a_refused_key(
    tmp_path, monkeypatch
):
    import pytest

    await _probe_with_status(tmp_path, monkeypatch, 400)  # key ok, clip refused
    with pytest.raises(RuntimeError):
        await _probe_with_status(tmp_path, monkeypatch, 401)


def test_probe_clip_ships_with_the_source():
    from src.bot.features.voice_handler import VoiceHandler

    data = VoiceHandler.PROBE_CLIP.read_bytes()
    assert data[:4] == b"OggS" and len(data) < 4096
