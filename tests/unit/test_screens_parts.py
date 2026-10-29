"""dv-4cd8 phase 1: the parts under the screen checks (width table, capture,
exclusive-pair rule, fixtures) — the acceptance cases are in
test_screens_checks.py / test_screens_gate.py."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import screens
from src.bot.screens import metrics
from src.bot.screens.capture import capture
from src.bot.screens.checks import check
from src.bot.screens.model import Button, Screen

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(screens.__file__).parent / "fixtures"


def _codes(rows, text="Pick one."):
    screen = Screen(
        "t",
        text,
        None,
        [[Button(label, data) for label, data in row] for row in rows],
        None,
    )
    return [v.code for v in check(screen)]


def test_widths_json_is_what_the_generator_writes():
    """The vendored table is regenerated, never hand-edited."""
    spec = importlib.util.spec_from_file_location(
        "screens_widths", REPO / "scripts" / "screens_widths.py"
    )
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)  # type: ignore[union-attr]
    if not all(p.exists() for p in gen.FONTS.values()):
        pytest.skip("Liberation Sans not installed on this host")
    assert gen.OUT.read_text(encoding="utf-8") == gen.build()


def test_calibration_matches_what_the_phone_showed():
    """Analysis §4: three to a row, "Claude Fable" truncates and "✓ Sonnet"
    fits; two to a row, "Codex GPT-5.6-Luna" truncates and "✓ GPT 5.6 Terra"
    fits. The phone showed "Clau…nnet / Clau…pus / Clau…able"."""
    assert metrics.is_truncated("Claude Fable", 3)
    assert not metrics.is_truncated("✓ Sonnet", 3)
    assert metrics.is_truncated("Codex GPT-5.6-Luna", 2)
    assert not metrics.is_truncated("✓ GPT 5.6 Terra", 2)
    shown = [metrics.visible_text(s, 3) for s in ("Claude Sonnet", "Claude Opus")]
    assert shown == ["Clau…nnet", "Clau…pus"]


def test_emoji_variation_selector_and_zwj_add_no_width():
    assert metrics.text_width("⚙️") == metrics.text_width("⚙")
    family = "👨‍👩‍👧"
    assert metrics.text_width(family) == metrics.text_width("👨")


def test_html_tags_do_not_count_toward_the_message_height():
    assert metrics.wrapped_lines("<b>Model</b> &amp; more", "HTML") == ["Model & more"]
    assert len(metrics.wrapped_lines("x " * 200)) > 1


def test_stepper_and_switch_pairs_are_not_exclusive():
    """−/+ on one setting and "switch to Claude/Codex" are meant together."""
    assert _codes([[("−", "set:pump:-"), ("+", "set:pump:+")]]) == []
    assert _codes([[("Claude", "lim:claude"), ("Codex", "lim:codex")]]) == []


def test_freeze_and_thaw_together_are_exclusive():
    codes = _codes([[("Freeze", "lim:freeze"), ("Thaw", "lim:thaw")]])
    assert codes == ["exclusive"]


def test_exclusive_falls_back_to_labels_without_callback_data():
    assert _codes([[("Show", None), ("Hide", None)]]) == ["exclusive"]


async def test_capture_records_a_reply_and_its_edits_in_order():
    async def handler(update, context):
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Go", callback_data="go")]]
        )
        await update.message.reply_text(
            "<b>Hi</b>", parse_mode="HTML", reply_markup=markup
        )
        await update.callback_query.edit_message_text("Done")
        await update.callback_query.edit_message_reply_markup(reply_markup=markup)

    shown = await capture(handler, screen_id="demo", goal="say hi")

    assert [s.id for s in shown] == ["demo", "demo.2", "demo.3"]
    assert [s.text for s in shown] == ["<b>Hi</b>", "Done", "Done"]
    assert shown[0].parse_mode == "HTML" and shown[0].goal == "say hi"
    assert [[b.text for b in r] for r in shown[0].rows] == [["Go"]]
    assert shown[1].rows == []
    assert all(s.render_ms is not None for s in shown)


async def test_capture_uses_a_given_update_and_context():
    seen = {}

    async def handler(update, context):
        seen["user"] = update.effective_user.id
        seen["data"] = context.user_data["k"]
        await update.message.reply_text("ok")

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7), message=SimpleNamespace()
    )
    context = SimpleNamespace(user_data={"k": 1}, bot_data={})
    shown = await capture(handler, update=update, context=context)

    assert seen == {"user": 7, "data": 1}
    assert [s.id for s in shown] == ["handler"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixtures_round_trip_and_name_their_source(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source"]
    screen = Screen.from_json(data)
    assert {k: v for k, v in data.items() if k != "source"} == screen.to_json()
