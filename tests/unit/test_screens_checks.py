"""dv-4cd8 phase 1: phone-width checks on bot screens.

Nothing used to look at a screen the way the phone shows it: dv-d63d's
/model passed review with three "Clau…" buttons in a row and Login + Logout
side by side; dv-6104's admin menu shipped with truncated ➖/➕ labels.
These tests pin the mechanical gate (``src/bot/screens``):

* ``model.Screen(id, text, parse_mode, rows, render_ms, goal="")`` with
  ``model.Button(text, callback_data)``; ``Screen.from_json(dict)`` loads a
  frozen fixture (one screen per file, ``src/bot/screens/fixtures/``).
* ``metrics.visible_text(label, n)`` — the label as Telegram shows it with
  ``n`` buttons in the row (middle ellipsis).
* ``checks.check(screen) -> list[Violation]``; ``Violation.code`` is one of
  truncated / duplicate / exclusive / row_width / too_long / slow.
* ``capture.capture(handler, update=None, context=None) -> list[Screen]``
  runs a handler with a fake ``reply_text`` and times it.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent_backends.codex_catalog import CodexModel
from src.bot.orchestrator import MessageOrchestrator

LIVE = [
    CodexModel("gpt-5.6-sol", "GPT-5.6-Sol", 272000, 4),
    CodexModel("gpt-5.6-terra", "GPT-5.6-Terra", 272000, 7),
    CodexModel("gpt-5.6-luna", "GPT-5.6-Luna", 272000, 8),
    CodexModel("gpt-5.5", "GPT-5.5", 272000, 12),
]

# The file whose existence /model reads as "Codex logged in" (a fake, empty).
_CODEX_LOGIN_FILE = "auth" + ".json"


def _fixture(name: str):
    from src.bot import screens
    from src.bot.screens.model import Screen

    path = Path(screens.__file__).parent / "fixtures" / name
    return Screen.from_json(json.loads(path.read_text(encoding="utf-8")))


def _screen(rows, text="Pick one.", render_ms=None):
    from src.bot.screens.model import Button, Screen

    return Screen(
        id="test.screen",
        text=text,
        parse_mode=None,
        rows=[[Button(label, f"cb:{label}") for label in row] for row in rows],
        render_ms=render_ms,
    )


def _codes(screen):
    from src.bot.screens.checks import check

    return [v.code for v in check(screen)]


def _labels(screen):
    return [[b.text for b in row] for row in screen.rows]


@pytest.fixture
def catalog(monkeypatch):
    from src.agent_backends import codex_catalog

    get = AsyncMock(return_value=list(LIVE))
    monkeypatch.setattr(codex_catalog.CODEX_CATALOG, "get", get)
    monkeypatch.setattr(
        codex_catalog.CODEX_CATALOG, "_models", list(LIVE), raising=False
    )
    return codex_catalog.CODEX_CATALOG


def _orchestrator():
    settings = SimpleNamespace(
        agentic_mode=True,
        agent_backend="claude",
        agent_model="claude",
        claude_model="sonnet",
    )
    return MessageOrchestrator(settings, {})


# --- acceptance fixtures -------------------------------------------------


def test_old_model_keyboard_7fca8f0_fails_truncated_and_exclusive():
    """The frozen dv-d63d /model (Claude backend, Codex logged in, the four
    live catalog models) fails: Claude labels truncate three to a row, and
    Login + Logout are shown together."""
    screen = _fixture("model_7fca8f0.json")
    labels = _labels(screen)
    # The fixture really is the 7fca8f0 keyboard, not a stand-in.
    assert labels[0] == ["Claude Sonnet", "Claude Opus", "Claude Fable"]
    assert labels[-1] == ["Login OpenAI Codex", "Logout Codex"]

    codes = _codes(screen)
    assert codes.count("truncated") >= 3
    assert "exclusive" in codes


@pytest.mark.parametrize("logged_in", [False, True], ids=["logged_out", "logged_in"])
async def test_current_model_keyboard_passes_every_check(
    catalog, monkeypatch, tmp_path, logged_in
):
    """df11ac2's /model, captured through the real handler, has no
    violation whether Codex is logged in or not."""
    from src.bot.screens.capture import capture

    if logged_in:
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / _CODEX_LOGIN_FILE).write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    orch = _orchestrator()
    context = SimpleNamespace(user_data={}, bot_data={})

    screens = await capture(orch.agentic_model, context=context)

    assert len(screens) == 1
    screen = screens[0]
    flat = [label for row in _labels(screen) for label in row]
    assert flat[:3] == ["✓ Sonnet", "Opus", "Fable"]
    assert flat[-1] == ("Log out of Codex" if logged_in else "Log in to Codex")
    assert screen.render_ms is not None and screen.render_ms < 2000
    assert _codes(screen) == []


@pytest.mark.parametrize(
    "name, marker",
    [("menu_6104_settings.json", "➖ "), ("menu_6104_pump.json", "⚡ ")],
    ids=["settings", "pump"],
)
def test_dv6104_menu_screens_fail_on_truncated_labels(name, marker):
    screen = _fixture(name)
    flat = [label for row in _labels(screen) for label in row]
    assert any(label.startswith(marker) for label in flat), flat
    assert "truncated" in _codes(screen)


# --- one case per check --------------------------------------------------


def test_two_labels_identical_after_truncation_fail_as_duplicate():
    from src.bot.screens.metrics import visible_text

    a = "Claude X-experimental-preview-build Sonnet"
    b = "Claude Y-experimental-preview-build Sonnet"
    # Different labels, same text on the phone (three to a row).
    assert visible_text(a, 3) == visible_text(b, 3)
    assert "…" in visible_text(a, 3)

    assert "duplicate" in _codes(_screen([[a, b, "Ok"]]))


def test_repeated_bare_steppers_under_their_labels_are_not_duplicates():
    """dv-f6af Настройки: "Параллельно задач: 5" then [−] [+], per setting —
    the bare −/+ repeat on purpose; any other repeated label still fails."""
    rows = [
        ["Параллельно задач: 5"],
        ["−", "+"],
        ["Запусков без прогресса: 12"],
        ["−", "+"],
    ]
    assert "duplicate" not in _codes(_screen(rows))
    assert "duplicate" in _codes(_screen([["Ok"], ["−", "+"], ["Ok"]]))


def test_five_buttons_in_a_row_fail_row_width():
    assert "row_width" in _codes(_screen([["1", "2", "3", "4", "5"]]))


def test_four_short_buttons_in_a_row_pass():
    assert _codes(_screen([["1", "2", "3", "4"]])) == []


def test_message_longer_than_one_phone_screen_fails_too_long():
    text = "\n".join(f"Line {i}: some status text" for i in range(80))
    assert "too_long" in _codes(_screen([["Ok"]], text=text))


def test_handler_over_two_seconds_fails_slow():
    assert "slow" in _codes(_screen([["Ok"]], render_ms=2500.0))


def test_handler_under_two_seconds_is_not_slow():
    assert "slow" not in _codes(_screen([["Ok"]], render_ms=1500.0))


def test_middle_ellipsis_keeps_head_and_tail_of_a_truncated_label():
    """The phone showed "Clau…able": head + … + tail, not a cut-off end."""
    from src.bot.screens.metrics import visible_text

    shown = visible_text("Claude Fable", 3)
    head, _, tail = shown.partition("…")
    assert shown != "Claude Fable"
    assert head and "Claude Fable".startswith(head)
    assert tail and "Claude Fable".endswith(tail)
    assert visible_text("Fable", 3) == "Fable"
