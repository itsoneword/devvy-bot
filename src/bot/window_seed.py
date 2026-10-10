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
from typing import Dict, List, Optional, Sequence, Tuple

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

# Cold start (dv-300e): a workspace with no resumable session starts a fresh
# one. Its seed is NOT bounded by a session — it is the workspace's stored
# history across whatever sessions the past week is spread over, because that
# is what the user experiences as "the conversation". Bounded twice: at most
# COLD_START_TAIL_TURNS turns, and never more than the token budget (the tail
# is trimmed whole-block from the front). Deeper than a rotation seed on
# purpose: a roll exists to RECLAIM context, a cold start has none to reclaim.
COLD_START_TAIL_DAYS = 7
COLD_START_TAIL_TURNS = 24
COLD_START_BUDGET_TOKENS = 12_000


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


def tools_line(tool_names: Sequence[str]) -> str:
    """Compact per-turn tool metadata for a rebuilt window (dv-168e): names
    only, deduped in first-call order with counts -- e.g.
    ``[tools: WebSearch×2, WebFetch]``. Empty when the turn used no tools."""
    counts: Dict[str, int] = {}
    for name in tool_names:
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    parts = [name if n == 1 else f"{name}×{n}" for name, n in counts.items()]
    return f"[tools: {', '.join(parts)}]"


def select_full_window(
    turns: Sequence[Tuple[str, str, Sequence[str]]], max_chars: int
) -> SeedTail:
    """Every stored turn of the CURRENT window, for a backend switch
    (dv-168e): conversation plus one tool-name metadata line after each
    turn's response that called tools. No turn count or tail budget: the
    window itself is bounded by the rotation threshold, so ``max_chars`` is
    only a backstop sized to that limit (the caller logs when it bites);
    trimming, if any, drops whole blocks from the front and is announced
    like any other trim."""
    rows: List[Tuple[str, str]] = []
    for prompt, response, tool_names in turns:
        meta = tools_line(tool_names)
        if meta:
            response = f"{response}\n{meta}" if response else meta
        rows.append((prompt, response))
    text, kept = _assemble_blocks(rows, max_chars)
    return SeedTail(
        text=text,
        shown_turns=kept,
        total_turns=len(rows),
        truncated=kept < len(rows),
    )


def select_history_tail(
    rows: Sequence[Tuple[str, str]],
    total_turns: Optional[int] = None,
    max_turns: int = COLD_START_TAIL_TURNS,
    budget_tokens: int = COLD_START_BUDGET_TOKENS,
) -> SeedTail:
    """Tail for a cold start: the last ``max_turns`` stored turns of a
    workspace, trimmed from the front until they fit ``budget_tokens``
    (est. chars/4, label overhead reserved).

    Unlike :func:`select_tail` there is no 5→3 step-down — the rows here span
    sessions and the caller has already limited how many it fetched, so the
    only question is how many of them fit.
    """
    candidate = list(rows[-max_turns:])
    max_chars = max(budget_tokens * 4 - SEED_LABEL_OVERHEAD_CHARS, 0)
    text, kept = _assemble_blocks(candidate, max_chars)
    return SeedTail(
        text=text,
        shown_turns=kept,
        total_turns=total_turns,
        truncated=kept < len(rows),
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

    ``reason="backend_switch"`` (dv-168e) labels a full rebuild of the
    current window for a fresh session on the other backend.

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
    if reason == "cold_start":
        return (
            "<conversation_context>\n"
            "This is a CONTINUATION of an ongoing relationship with this user, "
            "not a fresh start. No live session for this workspace could be "
            "resumed, so a new one was started; the transcript below was "
            "rebuilt from this workspace's stored history of recent days "
            f"(it may span several earlier sessions) and {scope}. Older turns "
            "still exist and are retrievable with recall_conversation. "
            "Durable facts and pointers to your earlier work are in your "
            "system prompt.\n"
            "</conversation_context>"
        )
    if reason == "backend_switch":
        if tail.text and tail.shown_turns == tail.total_turns:
            scope = f"all {tail.total_turns} turns of it appear below"
        return (
            "<conversation_context>\n"
            "This is a CONTINUATION of the current conversation, not a fresh "
            "start. The user switched to a different model backend, so a new "
            "session was started for you; this conversation window was rebuilt "
            f"in full from stored history and {scope}. Earlier replies in it "
            "were written by the other model, not by you. A [tools: ...] line "
            "after a reply names the tools that turn called (names only; their "
            "inputs and outputs are not carried over). Older windows are "
            "retrievable with recall_conversation. Durable facts and pointers "
            "to your earlier work are in your system prompt.\n"
            "</conversation_context>"
        )
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


_BACKEND_NAMES = {"claude": "Claude", "openai_codex": "OpenAI Codex"}


def _model_label(backend: str, model: Optional[str]) -> str:
    name = _BACKEND_NAMES.get(backend, backend)
    # "claude" is the stored sentinel for "no tier: config default".
    if not model or model == "claude":
        return f"{name} (default model)"
    return f"{name} ({model})"


def model_switch_note(
    prev: Tuple[str, Optional[str]], new: Tuple[str, Optional[str]]
) -> str:
    """The switch-point marker queued for the next turn (dv-168e)."""
    before = _model_label(*prev)
    after = _model_label(*new)
    return (
        "<model_switch>\n"
        f"The user switched the model at this point in the conversation: "
        f"{before} -> {after}. Replies before this point (including any "
        f"transcript carried over in this message) were written by {before}, "
        f"not by you. From here on you are {after}.\n"
        "</model_switch>"
    )
