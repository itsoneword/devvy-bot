"""Recipe search-retry detector + prompt (dv-300d / dv-5d5f follow-up).

Measured failure (dv-5d5f): the chef profile writes recipes from memory
without searching — 78 of 81 historical chef recipe turns had zero
WebSearch/WebFetch calls (dv-300d Tier-1 count, verified against bot.db). An
LLM judge cannot be trusted as the trigger for a fix: the dv-300d validation
gate showed a judge with no search of its own scores a confidently wrong
recipe as highly as a correct one. The trigger here is therefore MECHANICAL,
identical in spirit to ``save_honesty.py``'s write-claim check: "the answer
looks like a recipe" (``llm_analytics.quality.looks_like_recipe`` — list
structure + >=2 unit-qualified quantities) AND "the turn made zero Web* tool
calls".

This module is PURE (no I/O, no storage, no network): ``needs_search_retry``
is a plain function over already-collected turn data so it stays unit
testable without mocking the SDK. The in-process hook that acts on it
(``_run_recipe_search_retry`` in ``turns.py``) owns all the I/O.

``looks_like_recipe`` lives in ``src.llm_analytics.quality`` rather than
being duplicated here — its import chain is ``re`` / ``sqlite3`` /
``collections`` / ``pathlib`` / ``structlog`` only (checked 2026-09-19: no
pandas/numpy/heavy analytics deps), so importing it directly is cheap.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence, Tuple

from ...llm_analytics.quality import looks_like_recipe

# Sent to the SAME profile session as a follow-up turn once a recipe answer
# with no web search is detected. Deliberately does not quote the previous
# answer verbatim -- it is still in the session's own context, so pointing at
# it ("твой предыдущий ответ") is enough and keeps this prompt short.
#
# dv-e7fa: every retry used to land as a second, separate Telegram message
# regardless of outcome (the old prompt's "say so in one short phrase" was
# routinely ignored -- measured 2026-10-10: 9/9 retries sent a ~2k char
# rewrite even when nothing had changed). The verdict tag below lets the
# delivery code (``_run_recipe_search_retry`` in turns.py) edit the original
# answer in place instead of sending a new message, in the common
# "everything checked out" case.
RECIPE_RETRY_PROMPT = (
    "Твой предыдущий ответ выглядит как рецепт, но ты написал его по памяти, "
    "не проверив ни одного источника. Перепроверь его: сначала найди в "
    "интернете авторитетную версию этого рецепта (WebSearch/WebFetch).\n\n"
    "Ответ дай в строго заданном формате. Первая строка должна содержать "
    "ровно [OK] или [CORRECTED] и больше ничего.\n"
    "— [OK], если рецепт в итоге верный: на второй строке одной короткой "
    "фразой назови источник, который это подтвердил, и больше ничего не "
    "пиши — не переписывай рецепт заново.\n"
    "— [CORRECTED], если нашлись расхождения: на второй строке одной "
    "короткой фразой напиши, что изменилось, а дальше дай полный "
    "исправленный рецепт с верными пропорциями и техникой там, где они "
    "расходятся с найденным."
)

# Prefixed onto the retry's own first line before delivery, so the user can
# tell it apart from the original answer. There is never an attempt 3 (this
# retry's answer is not itself re-checked), hence "2 из 2" rather than a
# counter.
RETRY_ATTEMPT_LABEL = "🔁 Попытка 2 из 2 — перепроверил с поиском:"

# Tool names count as "searched" iff they start with this prefix -- covers
# WebSearch, WebFetch, and any future Web* addition without an allowlist to
# maintain.
_WEB_TOOL_PREFIX = "Web"


def needs_search_retry(response: str, tool_names: Sequence[str]) -> bool:
    """True iff ``response`` looks like a written-out recipe and no tool in
    ``tool_names`` is a web-search/fetch tool.

    Pure and I/O-free: both the live hook and any offline measurement script
    must call this exact function, or measured trigger rates stop meaning
    anything about production behaviour (same discipline as
    ``save_honesty.detect_write_claim`` / ``check_turn``).
    """
    if not looks_like_recipe(response):
        return False
    return not any(name.startswith(_WEB_TOOL_PREFIX) for name in tool_names)


# The tag must be the ONLY thing on the first line (optionally wrapped in
# markdown bold, which some models add on their own even when told not to
# format the tag). Anything else on that line -- trailing text, a missing
# tag, or the tag appearing on a later line -- is not a recognized verdict;
# the caller falls back to today's behaviour (a separate message) rather
# than guess at a malformed response.
_VERDICT_LINE_RE = re.compile(
    r"^(?:\*\*|__)?\[(OK|CORRECTED)\](?:\*\*|__)?$", re.IGNORECASE
)


def parse_retry_verdict(
    text: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse the retry turn's verdict tag out of ``text``.

    Returns ``(verdict, summary_line, body)``:

    - ``verdict``: ``"ok"``, ``"corrected"``, or ``None`` if the first line
      isn't exactly (whitespace/markdown-bold aside) ``[OK]`` or
      ``[CORRECTED]``.
    - ``summary_line``: the first non-blank line after the tag (the source
      name for ``"ok"``, the "what changed" note for ``"corrected"``), or
      ``None`` if there is no verdict or nothing follows the tag.
    - ``body``: for ``"corrected"``, everything after ``summary_line``
      (the full corrected recipe), stripped. ``""`` for ``"ok"`` (the model
      is told to write nothing else) or when there is no further text.

    Pure and I/O-free, like ``needs_search_retry`` -- the delivery code in
    ``turns.py`` owns what happens with the result (edit vs. send).
    """
    if not text:
        return None, None, None
    lines = text.splitlines()
    match = _VERDICT_LINE_RE.match(lines[0].strip())
    if not match:
        return None, None, None
    verdict = match.group(1).lower()

    rest = lines[1:]
    idx = 0
    while idx < len(rest) and not rest[idx].strip():
        idx += 1
    if idx >= len(rest):
        # Tag present but nothing follows -- too malformed to act on.
        return None, None, None

    summary_line = rest[idx].strip()
    body = "\n".join(rest[idx + 1 :]).strip()
    return verdict, summary_line, body
