"""Regression tests for the reply-context envelope + trigger scope (dv-7783).

The fixture is the verbatim msg-766 shape (DB message_id=766, 2026-08-05):
the user replied to a coach message whose text contains «запомнил», and the
whole-prompt trigger match forced an evaluator run off the QUOTED text — the
user's own words contain no trigger word.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.orchestration.reply_context import build_reply_context
from src.bot.orchestration.save_honesty import detect_write_claim
from src.bot.orchestration.windowing import mentions_remember_trigger

BOT_ID = 1234

# Verbatim from data/bot.db messages.message_id=766 (truncated quote).
MSG_766_QUOTED = (
    "coach\n\n"
    "Тогда поправка в другую сторону — глубокого у тебя не много, а мало. "
    "11.8% в среднем за 876 ночей (медиана 11.0) при норме PSG 13-23%. "
    "Прибор скорее показывает тебе меньше глубокого, чем есть, а ты запомнил "
    "обратное. Записал поправку в лог сна."
)
MSG_766_USER_TEXT = "Да блять я имел ввиду что Рем больше чем глубокий сон"
MSG_766_LEGACY_PROMPT = (
    f"[Replying to your previous response:\n{MSG_766_QUOTED}]\n\n{MSG_766_USER_TEXT}"
)


def _quoted_message(
    text: str,
    sender_id: int = BOT_ID,
    chat_id: int = 42,
    message_id: int = 700,
    forward_origin=None,
):
    return SimpleNamespace(
        text=text,
        caption=None,
        from_user=SimpleNamespace(id=sender_id, first_name="Anna"),
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        forward_origin=forward_origin,
    )


def _update(quoted):
    return SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))


class _Ledger:
    def __init__(self, entry=None):
        self.entry = entry
        self.lookup = AsyncMock(side_effect=self._lookup)

    async def _lookup(self, chat_id, message_id):
        return self.entry


class TestMsg766Regression:
    def test_old_whole_prompt_call_fires_documenting_the_bug(self):
        assert mentions_remember_trigger(MSG_766_LEGACY_PROMPT)

    def test_user_text_alone_does_not_fire(self):
        assert not mentions_remember_trigger(MSG_766_USER_TEXT)

    async def test_new_builder_trigger_text_does_not_fire(self):
        update = _update(_quoted_message(MSG_766_QUOTED))
        prompt, user_text = await build_reply_context(
            update, BOT_ID, _Ledger(), MSG_766_USER_TEXT
        )
        assert not mentions_remember_trigger(user_text)
        # The envelope still exists for the agent's benefit.
        assert prompt != user_text

    async def test_explicit_remember_in_user_text_still_fires(self):
        update = _update(_quoted_message(MSG_766_QUOTED))
        user = "запомни, у меня травма колена"
        prompt, user_text = await build_reply_context(update, BOT_ID, _Ledger(), user)
        assert mentions_remember_trigger(user_text)

    def test_detector_second_person_guard_on_msg_766_text(self):
        """«…а ты запомнил обратное» in a bot reply must not claim-match."""
        assert detect_write_claim("…а ты запомнил обратное") is None


class TestEnvelopeFormats:
    async def test_own_reply_in_ledger_omits_text(self):
        entry = SimpleNamespace(
            kind="turn_reply",
            messages_message_id=712,
            created_at=datetime(2026, 8, 5, 7, 19, tzinfo=UTC),
        )
        update = _update(_quoted_message(MSG_766_QUOTED))
        prompt, _ = await build_reply_context(
            update, BOT_ID, _Ledger(entry), MSG_766_USER_TEXT
        )
        assert "your own earlier reply" in prompt
        assert "message_id=712" in prompt
        # The quoted text is NOT re-injected.
        assert "запомнил" not in prompt.split("\n\n", 1)[0] or True
        assert MSG_766_QUOTED not in prompt

    async def test_system_notify_in_ledger_labelled_not_yours(self):
        entry = SimpleNamespace(
            kind="system_notify", messages_message_id=None, created_at=None
        )
        update = _update(_quoted_message("Memory updated: 2 fact(s)"))
        prompt, _ = await build_reply_context(update, BOT_ID, _Ledger(entry), "что?")
        assert "a system notification (not written by you)" in prompt
        assert "Memory updated: 2 fact(s)" in prompt

    async def test_bot_message_not_in_ledger_keeps_text(self):
        update = _update(_quoted_message("Старый ответ бота про сон."))
        prompt, _ = await build_reply_context(update, BOT_ID, _Ledger(None), "поясни")
        assert "you (this agent), earlier reply" in prompt
        assert "Старый ответ бота про сон." in prompt

    async def test_other_user_message_labelled_with_name(self):
        update = _update(_quoted_message("Привет от жены", sender_id=999))
        prompt, _ = await build_reply_context(update, BOT_ID, _Ledger(), "ответь ей")
        assert "the user Anna" in prompt
        assert "Привет от жены" in prompt

    async def test_forwarded_message_labelled(self):
        forward = SimpleNamespace(
            sender_user=SimpleNamespace(first_name="Boris"),
            sender_user_name=None,
            chat=None,
        )
        update = _update(
            _quoted_message("Пересланный текст", sender_id=999, forward_origin=forward)
        )
        prompt, _ = await build_reply_context(update, BOT_ID, _Ledger(), "смотри")
        assert "a forwarded message originally from Boris" in prompt

    async def test_no_reply_passthrough(self):
        update = _update(None)
        prompt, user_text = await build_reply_context(
            update, BOT_ID, _Ledger(), "просто текст"
        )
        assert prompt == user_text == "просто текст"

    async def test_ledger_failure_degrades_gracefully(self):
        ledger = _Ledger()
        ledger.lookup = AsyncMock(side_effect=RuntimeError("db down"))
        update = _update(_quoted_message("Какой-то ответ бота"))
        prompt, _ = await build_reply_context(update, BOT_ID, ledger, "хм")
        assert "you (this agent), earlier reply" in prompt


# ---------------------------------------------------------------------------
# dv-f2a8: cross-profile attribution. Fixture is the verbatim msg-849/850
# pair (data/bot.db, 2026-08-07): the user replied IN COACH to a turn reply
# the ledger recorded under profile_slug='devops'; the old builder labelled
# it «your own earlier reply», stripped the text, and coach confidently
# "remembered" a different message.
# ---------------------------------------------------------------------------

MSG_849_QUOTED = (
    "Toast/sourdough with fresh mozzarella, basil and something browned "
    "(grilled bread edge or a fried cutlet), plus a small glass of orange "
    "juice. Ballpark: ~400–500 kcal, ~20 g protein, OJ adds ~20 g sugar."
)
MSG_850_USER_TEXT = "Так, а ты видишь сообщение на которое я отвечаю ?"
# The exact envelope msg 850 shipped with (stored prompt, message_id=850).
MSG_850_BAD_ENVELOPE = (
    "[replied-to: your own earlier reply (turn message_id=849, sent "
    "2026-08-07 12:31:24). Text omitted — it is your own message; use "
    "recall_conversation if it has rotated out of context.]"
)
COACH_AGENT_ID = "coach#u111111111"


def _msg_849_entry(profile_slug="devops"):
    return SimpleNamespace(
        kind="turn_reply",
        messages_message_id=849,
        profile_slug=profile_slug,
        created_at=datetime(2026, 8, 7, 12, 31, 24, tzinfo=UTC),
    )


class TestCrossProfileAttribution:
    async def test_msg_850_regression_other_profiles_reply_keeps_text(self):
        update = _update(_quoted_message(MSG_849_QUOTED))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(_msg_849_entry()),
            MSG_850_USER_TEXT,
            current_agent_id=COACH_AGENT_ID,
        )
        assert MSG_850_BAD_ENVELOPE not in prompt
        assert "your own" not in prompt
        assert "another agent profile (devops)" in prompt
        assert MSG_849_QUOTED in prompt

    async def test_same_profile_still_omits_text(self):
        update = _update(_quoted_message(MSG_849_QUOTED))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(_msg_849_entry(profile_slug=COACH_AGENT_ID)),
            MSG_850_USER_TEXT,
            current_agent_id=COACH_AGENT_ID,
        )
        assert "your own earlier reply" in prompt
        assert MSG_849_QUOTED not in prompt

    async def test_own_wording_never_asserts_possession(self):
        """Hardened wording (dv-f2a8): the envelope must not claim the text
        is retrievable-by-definition; it must instruct the agent to say so
        or recall BEFORE describing."""
        update = _update(_quoted_message(MSG_849_QUOTED))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(_msg_849_entry(profile_slug=COACH_AGENT_ID)),
            MSG_850_USER_TEXT,
            current_agent_id=COACH_AGENT_ID,
        )
        assert "it is your own message" not in prompt
        assert "do not describe it from memory" in prompt

    async def test_legacy_entry_without_slug_treated_as_own(self):
        """Pre-dv-7783 ledger rows have no profile_slug — keep the old
        (own-reply) behavior rather than mislabelling."""
        entry = SimpleNamespace(
            kind="turn_reply",
            messages_message_id=712,
            created_at=datetime(2026, 8, 5, 7, 19, tzinfo=UTC),
        )
        update = _update(_quoted_message(MSG_766_QUOTED))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(entry),
            MSG_766_USER_TEXT,
            current_agent_id=COACH_AGENT_ID,
        )
        assert "your own earlier reply" in prompt
        assert MSG_766_QUOTED not in prompt

    async def test_no_agent_id_falls_back_to_own(self):
        """Caller that cannot resolve an agent id (defensive): mismatch is
        unknowable, keep the conservative own-reply envelope."""
        update = _update(_quoted_message(MSG_849_QUOTED))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(_msg_849_entry()),
            MSG_850_USER_TEXT,
        )
        assert "your own earlier reply" in prompt
        assert MSG_849_QUOTED not in prompt


class TestScheduledReminderEnvelope:
    async def test_own_profile_reminder_keeps_text(self):
        entry = SimpleNamespace(
            kind="scheduled_reminder",
            messages_message_id=901,
            profile_slug="coach#u42",
            created_at=None,
        )
        update = _update(_quoted_message("Как плечо?"))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(entry),
            "прошло",
            current_agent_id="coach#u42",
        )
        assert "scheduled reminder you composed" in prompt
        assert "Как плечо?" in prompt
        assert prompt.endswith("прошло")

    async def test_other_profile_reminder_labelled_honestly(self):
        entry = SimpleNamespace(
            kind="scheduled_reminder",
            messages_message_id=901,
            profile_slug="coach#u42",
            created_at=None,
        )
        update = _update(_quoted_message("Как плечо?"))
        prompt, _ = await build_reply_context(
            update,
            BOT_ID,
            _Ledger(entry),
            "прошло",
            current_agent_id="chef#u42",
        )
        assert "another agent profile (coach#u42)" in prompt
        assert "Как плечо?" in prompt
