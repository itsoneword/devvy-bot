"""dv-bf1e / dv-b022 — first-run gate, /setup and the interview's outputs."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import COMMAND_TABLE, OWNER_ONLY_COMMANDS, MessageOrchestrator
from src.config import create_test_config
from src.onboarding import core
from src.utils.constants import PERSONA_UNCONFIGURED_MARKER

OWNER = 4242
TEMPLATE = (
    f"{PERSONA_UNCONFIGURED_MARKER}\n# Agent Identity\n\nPlaceholder.\n\n"
    "## Environment\n\nHost facts stay.\n"
)


class Harness:
    def __init__(self, tmp_path: Path, persona: str = TEMPLATE) -> None:
        self.dir = tmp_path
        self.persona = tmp_path / "data" / "agent.md"
        self.persona.parent.mkdir()
        self.persona.write_text(persona, encoding="utf-8")
        self.config_file = tmp_path / "cfg.env"
        self.config_file.write_text("A=1\n", encoding="utf-8")
        self.settings = create_test_config(
            approved_directory=str(tmp_path),
            allowed_users=[OWNER],
            soul_md_path=str(self.persona.parent / "soul.md"),
        )
        self.writer = MagicMock()
        self.writer.apply_ops = AsyncMock(return_value=SimpleNamespace(added=3))
        self.orch = MessageOrchestrator(
            self.settings,
            {
                "claude_integration": MagicMock(),
                "storage": MagicMock(),
                "rate_limiter": None,
                "audit_logger": None,
            },
        )
        self.turn = AsyncMock()
        self.orch._agentic_text_turn = self.turn
        self.sent: list = []
        self.bot_data = {
            "settings": self.settings,
            "env_file": self.config_file,
            "agent_memory_writer": self.writer,
            "audit_logger": None,
            "storage": MagicMock(),
        }

    def _message(self, text=None):
        msg = MagicMock()
        msg.text = text

        async def reply(text, *a, **k):
            self.sent.append(text)
            return MagicMock()

        msg.reply_text = AsyncMock(side_effect=reply)
        return msg

    def _ctx(self, role="owner"):
        ctx = MagicMock()
        ctx.bot_data = self.bot_data
        ctx.user_data = {"role": role}
        return ctx

    def _update(self, message=None, query=None):
        update = MagicMock()
        update.message = message
        update.callback_query = query
        update.effective_user.id = OWNER
        update.effective_user.language_code = "ru"
        return update

    async def say(self, text, role="owner"):
        self.sent.clear()
        await self.orch.agentic_text(self._update(self._message(text)), self._ctx(role))

    async def press(self, data):
        self.sent.clear()
        q = MagicMock()
        q.data = data
        q.answer = AsyncMock()
        q.from_user.id = OWNER
        q.message = self._message()
        q.edit_message_text = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        await self.orch._agentic_callback(self._update(query=q), self._ctx())

    def text(self):
        return "\n".join(map(str, self.sent))


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# ----------------------------------------------------------------- the gate


def test_configured_check_reads_the_marker_only(tmp_path):
    h = Harness(tmp_path)
    assert core.is_unconfigured({}, h.settings)
    h.persona.write_text("# Agent Identity\nYou are Bob.\n")
    assert not core.is_unconfigured({}, h.settings)
    h.persona.unlink()
    assert not core.is_unconfigured({}, h.settings)  # never trap on a read error


def test_setup_is_a_table_command_and_owner_only():
    assert "setup" in {c.name for c in COMMAND_TABLE}
    assert "setup" in OWNER_ONLY_COMMANDS


async def test_unconfigured_owner_text_goes_to_interview_not_a_turn(h):
    await h.say("hello")
    h.turn.assert_not_awaited()
    assert "Давай настроим" in h.text()


async def test_guest_and_configured_owner_get_normal_turns(tmp_path):
    h = Harness(tmp_path)
    await h.say("hi", role="guest")
    assert h.turn.await_count == 1
    h.persona.write_text("# Agent Identity\nYou are Bob.\n")
    await h.say("hi")
    assert h.turn.await_count == 2


async def test_start_unconfigured_offers_setup_button(h):
    msg = h._message("/start")
    update = h._update(msg)
    update.effective_user.first_name = "Bob"
    await h.orch.agentic_start(update, h._ctx())
    assert "не настроен" in h.text()
    markup = msg.reply_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "onb:go"


# ------------------------------------------------------ interview -> outputs


async def test_preset_flow_writes_every_output_then_routes_normally(h):
    await h.say("hello")  # gate: intro
    await h.press("onb:preset:personal")  # tone + agents pre-filled
    assert "Как к тебе" in h.text()
    await h.say("Bob")  # name
    await h.press("onb:lang:Russian")
    await h.say("Not/AZone")  # rejected, asked again
    assert "Не узнаю" in h.text()
    await h.say("Europe/Lisbon")
    await h.press("onb:skip")  # assistant name -> default
    await h.press("onb:skip")  # tone: preset already set it, skip is harmless
    await h.press("onb:pb:y")
    await h.press("onb:agdone")
    await h.say("I run a small bakery")  # facts
    assert "Проверь" in h.text() and "Bob" in h.text()
    assert h.persona.read_text().startswith(PERSONA_UNCONFIGURED_MARKER)

    await h.press("onb:ok")

    persona = h.persona.read_text()
    assert PERSONA_UNCONFIGURED_MARKER not in persona
    assert "talking to Bob" in persona and "Reply in Russian" in persona
    assert "Host facts stay." in persona  # rest of the template survives
    assert "USER_TIMEZONE=Europe/Lisbon" in h.config_file.read_text()
    assert h.settings.user_timezone == "Europe/Lisbon"
    agent_id, ops = h.writer.apply_ops.await_args.args
    assert agent_id == "devops"
    contents = [o["content"] for o in ops]
    assert "The user's name is Bob." in contents
    assert "I run a small bakery" in contents
    assert all(o["kind"] == "user_fact" for o in ops)
    assert "Готово" in h.text()

    h.turn.reset_mock()
    await h.say("normal question")  # e2e: after completion, normal routing
    h.turn.assert_awaited_once()


async def test_free_form_extracts_then_asks_only_what_is_missing(h, monkeypatch):
    seen = {}

    async def fake_complete(prompt, **kw):
        seen["purpose"] = kw["purpose"]
        return 'sure: {"name": "Ann", "language": "English", "tone": "formal"}'

    monkeypatch.setattr(core, "complete_text", fake_complete)
    await h.say("hello")
    await h.say("I'm Ann, answer in English, formal please")
    assert seen["purpose"] == "onboarding"
    text = h.text()
    assert "Не хватает" in text and "часовой пояс" in text
    assert "язык ответов" not in text and "как к тебе" not in text

    await h.say("whatever")  # follow-up answered; mock repeats, nothing new
    assert "Проверь" in h.text()
    await h.press("onb:ok")
    persona = h.persona.read_text()
    assert "Ann" in persona and "Formal and precise" in persona


async def test_extraction_failure_degrades_to_step_by_step(h, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(core, "complete_text", boom)
    await h.say("hello")
    await h.say("blah blah")  # free form fails -> still progresses
    assert "Не хватает" in h.text()


async def test_skipping_everything_still_configures_with_defaults(h):
    await h.say("hello")
    await h.press("onb:next")
    for _ in range(8):
        await h.press("onb:skip")
    assert "Проверь" in h.text()
    await h.press("onb:ok")
    persona = h.persona.read_text()
    assert PERSONA_UNCONFIGURED_MARKER not in persona
    assert "You are Assistant" in persona
    h.writer.apply_ops.assert_not_awaited()  # no facts, nothing invented


async def test_setup_rerun_backs_up_a_tuned_persona(tmp_path):
    h = Harness(tmp_path, persona="# Agent Identity\nYou are Bob.\n\n## X\nkeep\n")
    msg = h._message("/setup")
    await h.orch._command_handlers()["setup"](h._update(msg), h._ctx())
    assert "переписан" in h.text()
    await h.press("onb:next")
    for _ in range(8):
        await h.press("onb:skip")
    await h.press("onb:ok")
    assert (h.persona.with_name("agent.md.bak")).read_text().startswith("# Agent")
    assert "You are Assistant" in h.persona.read_text()


async def test_non_owner_cannot_press_onboarding_buttons(h):
    q = MagicMock()
    q.data = "onb:ok"
    q.answer = AsyncMock()
    q.from_user.id = 999
    q.edit_message_text = AsyncMock()
    ctx = h._ctx(role="guest")
    await h.orch._agentic_callback(h._update(query=q), ctx)
    assert "Owner only" in q.edit_message_text.await_args.args[0]


# ------------------------------------------------------------------- agents


def test_install_agents_appends_profiles_via_the_installer(tmp_path):
    cfg = tmp_path / "profiles.yaml"
    failed = core.install_agents(["chef", "nope-not-a-template"], cfg)
    assert failed == ["nope-not-a-template"]
    assert 'slug: "chef"' in cfg.read_text()


def test_parse_extraction_ignores_junk_and_invalid_values():
    a = core.Answers()
    bad = {"timezone": "Mars/Base", "tone": "rude", "agents": ["x", "chef"]}
    core.merge_extraction(a, core.parse_extraction(json.dumps(bad)))
    assert a.timezone is None and a.tone is None and a.agents == ["chef"]
    assert core.parse_extraction("no json") == {}
