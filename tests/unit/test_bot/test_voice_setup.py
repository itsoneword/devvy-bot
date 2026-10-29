"""dv-6b6c — /voice setup: pick the STT provider, validate, switch live, persist.

Contract these tests pin (from docs/research/dv-6b6c/analysis.md §3):

- ``/voice`` is a COMMAND_TABLE command (owner-only). Its buttons carry
  ``vset:<provider>`` callback data — ``vset:elevenlabs | vset:openai |
  vset:mistral | vset:whisper | vset:off`` — dispatched by
  ``MessageOrchestrator._agentic_callback``.
- A cloud choice with no resolvable key replies with the exact ``/setkey``
  line; ``/setkey <provider> <PROVIDER>_API_KEY <key>`` then validates the key
  with one tiny transcription and, only if it passes, switches live.
- The switch mutates the shared ``Settings`` object and the voice handler in
  ``bot_data["features"]`` (the live ``FeatureRegistry``) — no restart.
- The choice is persisted into the config ``.env``: ``bot_data["env_file"]``
  when set, else ``.env`` in the cwd (what ``load_config`` reads). Tests set
  both to the same tmp file. Keys never go into ``.env``.

External calls are faked at the edges only: ``faster_whisper.WhisperModel``
and the ElevenLabs HTTP endpoint (``httpx.MockTransport``).
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.bot.features.registry import FeatureRegistry
from src.bot.orchestrator import COMMAND_TABLE, OWNER_ONLY_COMMANDS, MessageOrchestrator
from src.config import create_test_config
from src.security.keystore import KeyStore

OWNER = 4242
GOOD_KEY = "xi-GOOD-key-0123456789abcdef"
BAD_KEY = "xi-BAD-key-0123456789abcdef"
VOICE_BYTES = b"fake-ogg-voice-message"  # the "real" voice note in these tests
WHISPER_TEXT = "привет из локального whisper"
ELEVEN_TEXT = "привет из elevenlabs"

_VOICE_ENV = (
    "VOICE_PROVIDER",
    "ENABLE_VOICE_MESSAGES",
    "ELEVENLABS_API_KEY",
    "OPENAI_API_KEY",
    "MISTRAL_API_KEY",
)


# --------------------------------------------------------------------------
# Fakes at the external edges


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel.

    The voice note bytes transcribe to WHISPER_TEXT; anything else (the
    probe's silent clip) yields no segments, as real VAD would on silence.
    """

    instances: list = []

    def __init__(self, *args, **kwargs) -> None:
        FakeWhisperModel.instances.append(self)
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        data = audio.read() if hasattr(audio, "read") else audio
        if isinstance(data, (bytes, bytearray)) and bytes(data) == VOICE_BYTES:
            return [_Segment(WHISPER_TEXT)], SimpleNamespace(language="ru")
        return [], SimpleNamespace(language="ru")


@pytest.fixture
def fake_whisper(monkeypatch):
    import faster_whisper

    FakeWhisperModel.instances = []
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    return FakeWhisperModel


@pytest.fixture
def fake_elevenlabs(monkeypatch):
    """Route every httpx.AsyncClient through a mock ElevenLabs STT endpoint.

    GOOD_KEY -> 200; the voice note transcribes to ELEVEN_TEXT, any other
    audio (the probe clip) to "" (silence). BAD_KEY / no key -> 401.
    """
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("xi-api-key")
        seen.append((request.url.path, key))
        if "speech-to-text" not in request.url.path:
            return httpx.Response(404, json={"detail": "not found"})
        if key != GOOD_KEY:
            return httpx.Response(
                401, json={"detail": {"status": "invalid_api_key", "message": "bad"}}
            )
        body = request.read()
        text = ELEVEN_TEXT if VOICE_BYTES in body else ""
        return httpx.Response(200, json={"text": text, "language_code": "rus"})

    real_client = httpx.AsyncClient

    class _MockedAsyncClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockedAsyncClient)
    return seen


# --------------------------------------------------------------------------
# Bot wiring


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated cwd + config .env; no voice settings leaking from the shell."""
    for name in _VOICE_ENV:
        monkeypatch.delenv(name, raising=False)
    saved_environ = dict(os.environ)
    workdir = tmp_path / "bot"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    env_file = workdir / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=test:token\n"
        "TELEGRAM_BOT_USERNAME=testbot\n"
        f"ALLOWED_USERS={OWNER}\n"
        "# voice\n",
        encoding="utf-8",
    )
    yield SimpleNamespace(tmp=tmp_path, env_file=env_file)
    # load_config exports the .env into os.environ; undo that.
    os.environ.clear()
    os.environ.update(saved_environ)


class Bot:
    """One live process: settings, features, keystore, orchestrator."""

    def __init__(self, env, *, keystore=None, **settings_overrides) -> None:
        projects = env.tmp / "projects"
        projects.mkdir(exist_ok=True)
        values = dict(
            approved_directory=str(projects),
            allowed_users=[OWNER],
            enable_voice_messages=True,
            voice_provider="whisper",
            whisper_model="tiny",
        )
        values.update(settings_overrides)
        self.settings = create_test_config(**values)
        self.features = FeatureRegistry(
            config=self.settings, storage=MagicMock(), security=MagicMock()
        )
        self.keystore = keystore or KeyStore(env.tmp / "secrets" / "tools")
        self.orchestrator = MessageOrchestrator(
            self.settings,
            {
                "claude_integration": MagicMock(),
                "storage": MagicMock(),
                "security_validator": MagicMock(),
                "rate_limiter": MagicMock(),
                "audit_logger": None,
            },
        )
        # The turn after transcription is not under test: capture its prompt.
        self.orchestrator._run_agentic_media_turn = AsyncMock()
        self.sent: list = []
        self.bot_data = {
            "settings": self.settings,
            "features": self.features,
            "keystore": self.keystore,
            "env_file": env.env_file,
            "audit_logger": None,
            "storage": MagicMock(),
        }

    # -- context / updates -------------------------------------------------

    def context(self, args=None):
        ctx = MagicMock()
        ctx.bot_data = self.bot_data
        ctx.user_data = {"role": "owner"}
        ctx.args = list(args or [])

        async def send_message(chat_id, text, **kwargs):
            self.sent.append(text)
            return MagicMock(edit_text=AsyncMock(side_effect=self._record_edit))

        ctx.bot.send_message = AsyncMock(side_effect=send_message)
        return ctx

    async def _record_edit(self, text, *args, **kwargs):
        self.sent.append(text)
        return MagicMock()

    def _message(self, text=None):
        msg = MagicMock()
        msg.text = text
        msg.caption = None
        msg.message_thread_id = None
        msg.delete = AsyncMock()
        msg.chat.send_action = AsyncMock()
        msg.chat.id = 42

        async def reply_text(text, *args, **kwargs):
            self.sent.append(text)
            return MagicMock(edit_text=AsyncMock(side_effect=self._record_edit))

        msg.reply_text = AsyncMock(side_effect=reply_text)
        return msg

    def _update(self, message=None, callback_query=None):
        update = MagicMock()
        update.message = message
        update.callback_query = callback_query
        update.effective_user.id = OWNER
        update.effective_chat.id = 42
        update.effective_message = message
        return update

    # -- the user's actions ------------------------------------------------

    async def command(self, name: str, text: str):
        handler = self.orchestrator._command_handlers()[name]
        parts = text.split()
        msg = self._message(text)
        update = self._update(message=msg)
        await handler(update, self.context(parts[1:]))
        await _settle()
        return msg

    async def press(self, data: str) -> None:
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.from_user.id = OWNER
        query.message = self._message()

        async def edit(text, *args, **kwargs):
            self.sent.append(text)
            return MagicMock()

        query.edit_message_text = AsyncMock(side_effect=edit)
        query.message.edit_text = AsyncMock(side_effect=edit)
        update = self._update(message=None, callback_query=query)
        update.effective_message = query.message
        await self.orchestrator._agentic_callback(update, self.context())
        await _settle()

    async def send_voice(self):
        voice = MagicMock()
        voice.file_id = "voice-1"
        voice.duration = 3
        voice.file_size = len(VOICE_BYTES)
        tg_file = MagicMock()
        tg_file.file_size = len(VOICE_BYTES)
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(VOICE_BYTES))
        voice.get_file = AsyncMock(return_value=tg_file)
        msg = self._message()
        msg.voice = voice
        self.orchestrator._run_agentic_media_turn.reset_mock()
        await self.orchestrator.agentic_voice(self._update(message=msg), self.context())
        await _settle()
        call = self.orchestrator._run_agentic_media_turn.await_args
        return call.kwargs["prompt"] if call else None

    def voice_handler(self):
        return self.bot_data["features"].get_voice_handler()

    def all_text(self) -> str:
        return "\n".join(str(t) for t in self.sent)


async def _settle() -> None:
    """Let any task the handler spawned (e.g. a background probe) finish."""
    for _ in range(50):
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=0.1)


def _env_values(env_file: Path) -> dict:
    out = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


# --------------------------------------------------------------------------
# Approved scenarios


async def test_voice_whisper_choice_transcribes_next_voice_without_restart(
    env, fake_whisper
):
    """/voice -> Local Whisper -> next voice message transcribed, no restart."""
    bot = Bot(env, voice_provider="elevenlabs", elevenlabs_api_key=None)
    assert bot.voice_handler() is None  # no key -> voice unavailable at boot
    settings_before = bot.settings

    await bot.command("voice", "/voice")
    await bot.press("vset:whisper")

    assert bot.settings is settings_before  # same live object, mutated
    assert bot.settings.voice_provider == "whisper"
    assert bot.settings.enable_voice_messages is True
    assert bot.voice_handler() is not None

    prompt = await bot.send_voice()
    assert prompt is not None, "voice message did not reach the turn"
    assert WHISPER_TEXT in prompt
    assert fake_whisper.instances, "local whisper was never used"


async def test_voice_elevenlabs_setkey_validates_switches_and_deletes_key(
    env, fake_whisper, fake_elevenlabs
):
    """/voice -> ElevenLabs -> /setkey -> key checked, switched, key msg deleted."""
    bot = Bot(env, voice_provider="whisper")

    await bot.command("voice", "/voice")
    await bot.press("vset:elevenlabs")
    # No key yet: the bot tells the owner the exact /setkey line to send.
    assert "/setkey elevenlabs ELEVENLABS_API_KEY" in bot.all_text()
    assert bot.settings.voice_provider == "whisper"  # nothing switched yet

    key_msg = await bot.command(
        "setkey", f"/setkey elevenlabs ELEVENLABS_API_KEY {GOOD_KEY}"
    )

    key_msg.delete.assert_awaited()  # the key leaves the chat
    assert GOOD_KEY not in bot.all_text()  # and is never echoed
    # Key checked: one real call to the provider with that key.
    assert any(k == GOOD_KEY for _, k in fake_elevenlabs), "key was not validated"
    # Switched live.
    assert bot.settings.voice_provider == "elevenlabs"
    assert bot.voice_handler() is not None
    prompt = await bot.send_voice()
    assert prompt is not None and ELEVEN_TEXT in prompt
    # Persisted provider, never the key.
    env_text = env.env_file.read_text(encoding="utf-8")
    assert _env_values(env.env_file).get("VOICE_PROVIDER") == "elevenlabs"
    assert GOOD_KEY not in env_text


async def test_bad_cloud_key_rejected_previous_provider_keeps_working(
    env, fake_whisper, fake_elevenlabs
):
    """Bad cloud key -> "ключ отклонён", previous provider keeps working."""
    bot = Bot(env, voice_provider="whisper")
    assert await bot.send_voice() is not None  # whisper works before

    await bot.command("voice", "/voice")
    await bot.press("vset:elevenlabs")
    await bot.command("setkey", f"/setkey elevenlabs ELEVENLABS_API_KEY {BAD_KEY}")

    assert any(k == BAD_KEY for _, k in fake_elevenlabs), "key was not validated"
    assert "ключ отклонён" in bot.all_text().lower()
    assert BAD_KEY not in bot.all_text()
    assert bot.settings.voice_provider == "whisper"
    assert _env_values(env.env_file).get("VOICE_PROVIDER") != "elevenlabs"

    prompt = await bot.send_voice()
    assert prompt is not None and WHISPER_TEXT in prompt


async def test_text_only_turns_voice_off_and_voice_message_points_to_voice(
    env, fake_whisper
):
    """ "Только текст" -> voice off; voice message gets "/voice to enable"."""
    bot = Bot(env, voice_provider="whisper")
    assert bot.voice_handler() is not None

    await bot.command("voice", "/voice")
    await bot.press("vset:off")

    assert bot.settings.enable_voice_messages is False
    assert bot.voice_handler() is None
    assert _env_values(env.env_file).get("ENABLE_VOICE_MESSAGES", "").lower() in (
        "false",
        "0",
    )

    bot.sent.clear()
    prompt = await bot.send_voice()
    assert prompt is None  # nothing transcribed, no turn
    assert "/voice" in bot.all_text()
    assert not any(m.calls for m in fake_whisper.instances)


async def test_voice_choice_survives_restart(env, fake_whisper):
    """Choice survives a bot restart."""
    from src.config import load_config

    bot = Bot(env, voice_provider="elevenlabs", elevenlabs_api_key=None)
    await bot.command("voice", "/voice")
    await bot.press("vset:whisper")
    assert bot.settings.voice_provider == "whisper"

    # "Restart": a fresh process loads the same .env.
    os.environ.pop("VOICE_PROVIDER", None)
    os.environ.pop("ENABLE_VOICE_MESSAGES", None)
    reloaded = load_config(config_file=env.env_file)
    assert reloaded.voice_provider == "whisper"
    assert reloaded.enable_voice_messages is True
    registry = FeatureRegistry(
        config=reloaded, storage=MagicMock(), security=MagicMock()
    )
    assert registry.get_voice_handler() is not None


# --------------------------------------------------------------------------
# Added scenarios


def test_voice_command_is_owner_only_and_its_buttons_are_routed(env):
    """added: /voice is in the table + owner-only, and vset: buttons reach a handler.

    The /auth buttons once shipped dead because the CallbackQueryHandler
    pattern lacked their prefix; this guards the same mistake for vset:.
    """
    from telegram.ext import CallbackQueryHandler

    assert "voice" in {spec.name for spec in COMMAND_TABLE}
    assert "voice" in OWNER_ONLY_COMMANDS

    bot = Bot(env)
    app = MagicMock()
    bot.orchestrator.register_handlers(app)
    patterns = [
        call[0][0].pattern
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]
    for data in ("vset:whisper", "vset:elevenlabs", "vset:off"):
        assert any(p.match(data) for p in patterns), f"no handler routes {data!r}"


async def test_cloud_key_from_keystore_survives_restart(
    env, monkeypatch, fake_whisper, fake_elevenlabs
):
    """added: ElevenLabs chosen with a /setkey key -> after restart voice still works.

    The key lives only in the keystore (never in .env / secrets), so the boot
    path must resolve it from there too, or a restart silently drops voice.
    """
    from src.config import load_config
    from src.security.keystore import default_keystore

    bot = Bot(env, voice_provider="whisper")
    # The live store lives under the data root; point the process at one.
    root = env.tmp / "root"
    root.mkdir()
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(root))
    store = default_keystore()
    assert store is not None
    bot.keystore = bot.bot_data["keystore"] = store

    await bot.command("voice", "/voice")
    await bot.press("vset:elevenlabs")
    await bot.command("setkey", f"/setkey elevenlabs ELEVENLABS_API_KEY {GOOD_KEY}")
    assert bot.settings.voice_provider == "elevenlabs"

    os.environ.pop("VOICE_PROVIDER", None)
    reloaded = load_config(config_file=env.env_file)
    assert reloaded.voice_provider == "elevenlabs"
    registry = FeatureRegistry(
        config=reloaded, storage=MagicMock(), security=MagicMock()
    )
    handler = registry.get_voice_handler()
    assert handler is not None, "keystore-only key not resolved at boot"


async def test_storing_key_for_other_provider_does_not_switch(
    env, fake_whisper, fake_elevenlabs
):
    """added: /setkey of a voice key while no switch to that provider is pending
    must not silently change the live provider."""
    bot = Bot(env, voice_provider="whisper")
    await bot.command("setkey", f"/setkey elevenlabs ELEVENLABS_API_KEY {GOOD_KEY}")

    assert bot.keystore.env("elevenlabs") == {"ELEVENLABS_API_KEY": GOOD_KEY}
    assert bot.settings.voice_provider == "whisper"
    prompt = await bot.send_voice()
    assert prompt is not None and WHISPER_TEXT in prompt
