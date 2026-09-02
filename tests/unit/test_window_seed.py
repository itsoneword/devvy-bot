import re

from src.bot.window_seed import (
    SEED_LABEL_OVERHEAD_CHARS,
    SEED_TAIL_BUDGET_TOKENS,
    SeedTail,
    assemble_tail,
    build_seed_preamble,
    select_tail,
)


def test_assemble_tail_orders_and_formats():
    rows = [("hi", "hello"), ("run df", "/dev 50%")]
    text = assemble_tail(rows, max_chars=10_000)
    assert text == "User: hi\nAssistant: hello\n\nUser: run df\nAssistant: /dev 50%"


def test_assemble_tail_hard_caps_from_the_end():
    rows = [("a" * 100, "b" * 100), ("recent-q", "recent-a")]
    text = assemble_tail(rows, max_chars=40)
    # keeps the most recent content, drops the oldest
    assert "recent-q" in text and "recent-a" in text
    assert len(text) <= 40
    assert "a" * 100 not in text


def test_assemble_tail_empty():
    assert assemble_tail([], max_chars=100) == ""


class TestSelectTail:
    """Adaptive tail: 5 turns when they fit ~10k tokens, else 3 regardless."""

    def test_small_rows_keep_five_turns(self):
        rows = [(f"q{i}", f"a{i}") for i in range(7)]
        tail = select_tail(rows, total_turns=7)
        for i in range(2, 7):
            assert f"q{i}" in tail.text
        assert "q1\n" not in tail.text and "q0" not in tail.text
        assert tail.shown_turns == 5
        assert tail.total_turns == 7
        assert tail.truncated is False

    def test_oversized_five_falls_back_to_three_and_flags_truncation(self):
        big = "x" * 10_000  # 5 turns × 20k chars ≈ 25k tokens > 10k budget
        rows = [(f"marker{i} {big}", big) for i in range(5)]
        tail = select_tail(rows, total_turns=40)
        assert "marker0" not in tail.text and "marker1" not in tail.text
        for i in (2, 3, 4):
            assert f"marker{i}" in tail.text
        assert tail.shown_turns == 3
        assert tail.truncated is True

    def test_label_overhead_counts_toward_budget(self):
        """Criterion 2: labels displace tail content, never add on top.

        Five turns that fit the raw budget but not budget-minus-overhead must
        fall back to three — the overhead reserve is part of the budget."""
        budget_chars = SEED_TAIL_BUDGET_TOKENS * 4
        per_turn = (budget_chars - SEED_LABEL_OVERHEAD_CHARS // 2) // 5
        rows = [(f"m{i} " + "y" * (per_turn - 20), "ok") for i in range(5)]
        raw = assemble_tail(rows, max_chars=1_000_000)
        assert len(raw) // 4 <= SEED_TAIL_BUDGET_TOKENS  # fits WITHOUT labels
        tail = select_tail(rows)
        assert tail.shown_turns == 3  # but not WITH them
        assert tail.truncated is True

    def test_empty_rows(self):
        tail = select_tail([], total_turns=0)
        assert tail.text == ""
        assert tail.shown_turns == 0
        assert tail.truncated is False


class TestBuildSeedPreamble:
    def test_wraps_tail_and_message_with_context_block(self):
        tail = SeedTail(text="User: hi\nAssistant: yo", shown_turns=1, total_turns=1)
        out = build_seed_preamble(tail, user_message="continue")
        assert out.startswith("<conversation_context>")
        assert "CONTINUATION" in out
        assert "recall_conversation" in out
        assert "User: hi" in out
        assert out.rstrip().endswith("continue")
        # kept verbatim so llm_analytics SEED_MARKERS / seeded classification
        # and the closing-instruction contract hold
        assert "Continue the conversation above. The user now says:" in out

    def test_no_tail_no_notes_returns_message_byte_identical(self):
        """Criterion 4: an ordinary (non-rotation) turn is unchanged."""
        tail = SeedTail(text="", shown_turns=0)
        assert build_seed_preamble(tail, user_message="hello") == "hello"

    def test_shown_of_total_in_context_block(self):
        tail = SeedTail(text="User: q", shown_turns=5, total_turns=47)
        out = build_seed_preamble(tail, user_message="go")
        assert "last 5 of its 47 turns" in out

    def test_trim_line_when_shown_below_total(self):
        tail = SeedTail(text="User: q", shown_turns=5, total_turns=47)
        out = build_seed_preamble(tail, user_message="go")
        assert (
            "[last 5 of 47 turns; earlier turns trimmed — "
            "use recall_conversation if needed]" in out
        )

    def test_trim_line_when_truncated_without_total(self):
        tail = SeedTail(text="User: q", shown_turns=3, truncated=True)
        out = build_seed_preamble(tail, user_message="go")
        assert (
            "[last 3 turns; earlier turns trimmed — "
            "use recall_conversation if needed]" in out
        )

    def test_no_trim_line_when_tail_is_complete(self):
        tail = SeedTail(text="User: q", shown_turns=2, total_turns=2)
        out = build_seed_preamble(tail, user_message="go")
        assert "earlier turns trimmed" not in out

    def test_session_notes_block_and_header(self):
        tail = SeedTail(text="User: q", shown_turns=1, total_turns=1)
        out = build_seed_preamble(
            tail,
            user_message="go",
            session_notes="About the user (dated [DD.MM.YY] = when each "
            "became true):\n- [04.08.26] height 185",
        )
        assert "<session_notes>" in out
        assert "Carried over from the earlier part of this conversation:" in out
        assert "- [04.08.26] height 185" in out
        # context block comes first, then notes, then transcript
        assert out.index("<conversation_context>") < out.index("<session_notes>")
        assert out.index("<session_notes>") < out.index("<recent_conversation>")

    def test_notes_only_seed_still_gets_context_block(self):
        tail = SeedTail(text="", shown_turns=0)
        out = build_seed_preamble(
            tail, user_message="go", session_notes="- [01.01.26] a fact"
        )
        assert out.startswith("<conversation_context>")
        assert "could not be carried over" in out
        assert "<recent_conversation>" not in out

    def test_preamble_stays_within_budget_estimate(self):
        """Criterion 2: when the 5-turn tail fits the budget with labels
        counted, the WHOLE preamble estimate stays under it. (The 3-turn
        fallback deliberately keeps 3 turns whatever their size — that
        pre-existing escape is unchanged and not asserted here.)"""
        big = "z" * 3_800  # 5 turns ≈ 38k chars + overhead < 40k-char budget
        rows = [(f"t{i} {big}", big) for i in range(5)]
        tail = select_tail(rows, total_turns=30)
        assert tail.shown_turns == 5
        out = build_seed_preamble(tail, user_message="")
        assert len(out) // 4 <= SEED_TAIL_BUDGET_TOKENS

    def test_labels_add_at_most_the_reserved_overhead(self):
        """The template's fixed labels are bounded by the budget reserve."""
        tail = SeedTail(
            text="User: q\nAssistant: a",
            shown_turns=3,
            total_turns=99,
            truncated=True,
        )
        out = build_seed_preamble(tail, user_message="")
        assert len(out) - len(tail.text) <= SEED_LABEL_OVERHEAD_CHARS

    def test_seed_markers_recognize_new_template(self):
        from src.llm_analytics.parser import is_seeded_text

        tail = SeedTail(text="User: q", shown_turns=1, total_turns=1)
        out = build_seed_preamble(tail, user_message="go")
        assert is_seeded_text(out)

    def test_dated_line_format_matches_owner_decision_d2(self):
        """Shared renderer emits [DD.MM.YY]; the seed must not reformat it."""
        notes = "- [04.08.26] height 185"
        tail = SeedTail(text="User: q", shown_turns=1, total_turns=1)
        out = build_seed_preamble(tail, user_message="go", session_notes=notes)
        assert re.search(r"\[\d{2}\.\d{2}\.\d{2}\] height 185", out)
