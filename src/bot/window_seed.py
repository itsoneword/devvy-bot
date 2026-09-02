"""Pure assembly of the cross-backend continuity seed (dv-e574).

A transition seeds the fresh backend with the un-memorized raw tail of the
previous window. Durable memory rides along via the frozen window bundle
(dv-5e2a), so the seed is only the recent transcript plus a session-notes
block — hard-capped to bound prompt size.

The seed is labeled honestly: it says it is a continuation, that older turns
exist and were dropped on purpose, and that ``recall_conversation`` retrieves
them. A silently shortened tail must never read as a complete conversation,
so truncation (the 5→3 fallback, or whole blocks trimmed by the char cap) is
announced inside ``<recent_conversation>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

SEED_MAX_CHARS = 120_000  # defensive cap (~tens of k tokens) for the tail

# How many recent turns of the outgoing window to carry across a transition.
# Small on purpose: the roll exists to RECLAIM context, so the seed is the
# last exchanges (what we were just discussing) plus the session-notes block
# (durable facts the evaluator extracted) — not a big raw replay. The old
# 12-turn tail could drag ~30k tokens straight into the fresh window.
# Adaptive: prefer 5 turns; if those 5 estimate over the token budget, fall
# back to 3 turns whatever their size (SEED_MAX_CHARS still backstops).
SEED_TAIL_TURNS = 5
SEED_TAIL_TURNS_FALLBACK = 3
SEED_TAIL_BUDGET_TOKENS = 10_000

# Upper bound on the template's own labels (<conversation_context>, the
# <recent_conversation> trim line, the closing instruction). Reserved inside
# the token budget so labels DISPLACE tail content rather than add to it
# (dv-e574 criterion 2: no regression against SEED_TAIL_BUDGET_TOKENS).
SEED_LABEL_OVERHEAD_CHARS = 800


@dataclass(frozen=True)
class SeedTail:
    """An assembled tail plus the honesty metadata the template renders.

    ``total_turns`` is the outgoing window's full turn count (None when the
    caller cannot know it); ``truncated`` means the tail itself was shortened
    (5→3 fallback fired, or ``assemble_tail`` dropped whole blocks) — distinct
    from ``shown_turns < total_turns``, which is true on almost every roll.
    """

    text: str
    shown_turns: int
    total_turns: Optional[int] = None
    truncated: bool = False


def select_tail(
    rows: Sequence[Tuple[str, str]],
    total_turns: Optional[int] = None,
    max_chars: int = SEED_MAX_CHARS,
) -> SeedTail:
    """Adaptive tail: last SEED_TAIL_TURNS turns if they (plus the template's
    label overhead) fit the token budget (est. chars/4), else the last
    SEED_TAIL_TURNS_FALLBACK turns regardless of their size."""
    candidate = list(rows[-SEED_TAIL_TURNS:])
    text, kept = _assemble_blocks(candidate, max_chars)
    truncated = kept < len(candidate)
    if (len(text) + SEED_LABEL_OVERHEAD_CHARS) // 4 > SEED_TAIL_BUDGET_TOKENS:
        candidate = list(rows[-SEED_TAIL_TURNS_FALLBACK:])
        text, kept = _assemble_blocks(candidate, max_chars)
        truncated = True
    return SeedTail(
        text=text, shown_turns=kept, total_turns=total_turns, truncated=truncated
    )


def assemble_tail(rows: Sequence[Tuple[str, str]], max_chars: int) -> str:
    """Format (prompt, response) rows oldest→newest, then keep only the most
    recent `max_chars` characters (drop whole-blocks from the front)."""
    text, _kept = _assemble_blocks(rows, max_chars)
    return text


def _assemble_blocks(
    rows: Sequence[Tuple[str, str]], max_chars: int
) -> Tuple[str, int]:
    """Assemble rows into transcript text; return (text, blocks_kept)."""
    blocks: List[str] = []
    for prompt, response in rows:
        block = f"User: {prompt}"
        if response:
            block += f"\nAssistant: {response}"
        blocks.append(block)
    if not blocks:
        return "", 0
    # Trim from the front until under the cap.
    while blocks and len("\n\n".join(blocks)) > max_chars:
        blocks.pop(0)
    if not blocks:
        # Single oversized block: hard-truncate its tail.
        return "", 0
    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        return text[-max_chars:], len(blocks)
    return text, len(blocks)


def _context_block(tail: SeedTail, reason: str = "rotation") -> str:
    """The honest-labelling header: continuation, deliberate drop, escape
    hatch. One block, owner's ask: 'this is prompt, these are facts, these
    are the last messages'.

    ``reason="resume_failed"`` (dv-b41f) swaps the "rotated deliberately"
    wording — false for an involuntary backend-session loss — for an honest
    "could not be resumed" one. The default stays byte-identical for every
    dv-e574 caller.
    """
    if tail.text and tail.total_turns:
        scope = (
            f"only the last {tail.shown_turns} of its {tail.total_turns} "
            "turns appear below"
        )
    elif tail.text:
        scope = f"only its last {tail.shown_turns} turns appear below"
    else:
        scope = "the recent transcript could not be carried over"
    if reason == "resume_failed":
        return (
            "<conversation_context>\n"
            "This is a CONTINUATION of a long-running conversation, not a "
            "fresh start. The previous session could not be resumed after an "
            f"interruption, so a fresh one was started automatically; {scope}. "
            "Older turns still exist and are retrievable with "
            "recall_conversation. Durable facts and pointers to your earlier "
            "work are in your system prompt.\n"
            "</conversation_context>"
        )
    return (
        "<conversation_context>\n"
        "This is a CONTINUATION of a long-running conversation, not a fresh "
        f"start. Your context was rotated: {scope}. Older turns still exist, "
        "were dropped deliberately, and are retrievable with "
        "recall_conversation. Durable facts and pointers to your earlier "
        "work are in your system prompt.\n"
        "</conversation_context>"
    )


def build_seed_preamble(
    tail: SeedTail, user_message: str, session_notes: str = "", reason: str = "rotation"
) -> str:
    """Wrap the tail (+ extracted session facts) as labeled context before
    the user's new message. No tail and no notes → the message unchanged
    (criterion 4: ordinary turns are byte-identical to today)."""
    if not tail.text and not session_notes:
        return user_message
    parts: List[str] = [_context_block(tail, reason)]
    if session_notes:
        parts.append(
            "<session_notes>\n"
            "Carried over from the earlier part of this conversation:\n"
            f"{session_notes}\n"
            "</session_notes>"
        )
    if tail.text:
        trimmed = tail.truncated or (
            tail.total_turns is not None and tail.shown_turns < tail.total_turns
        )
        trim_note = ""
        if trimmed:
            shown_of = (
                f"last {tail.shown_turns} of {tail.total_turns} turns"
                if tail.total_turns
                else f"last {tail.shown_turns} turns"
            )
            trim_note = (
                f"[{shown_of}; earlier turns trimmed — "
                "use recall_conversation if needed]\n"
            )
        parts.append(
            f"<recent_conversation>\n{trim_note}{tail.text}\n</recent_conversation>"
        )
    return (
        "\n\n".join(parts)
        + "\n\nContinue the conversation above. The user now says:\n\n"
        + user_message
    )
