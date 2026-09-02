"""Save-honesty detector (dv-7783): did the reply claim a write that happened?

Measured failure: 64% of coach food-mention turns emitted «записал» with zero
tool calls (docs/FINDINGS.md 2026-08-04). Decided response is telemetry only —
detect + log + inform the owner; no auto-correction, no retry, no reply
annotation.

This module is PURE (no I/O, no storage): the in-process hook in ``turns.py``
and the offline FP-measurement script (``scripts/save_honesty_report.py``)
must run the *identical* code, or the measured false-positive rate says
nothing about production behaviour.

Pattern families are module constants tuned by the offline FP run; each match
reports its family so ``matched_pattern`` is loggable and recomputable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

# Tools whose presence in a turn satisfies a write-claim. ``Bash`` counts
# (owner decision D12): agents legitimately write files via shell; excluding
# it is a guaranteed false-positive class. It is recorded distinctly in
# ``satisfied_by`` so "satisfied only by Bash" stays analyzable. The dv-a9e1
# audit tools are listed from day one — matching a tool that doesn't exist
# yet is harmless.
WRITE_TOOLS = {
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "mcp__agent__remember",
    "mcp__agent__task_add",
    "mcp__agent__schedule_add",
    "mcp__agent__memory_forget",
    "mcp__agent__memory_correct",
    # Channel-KB writes (the channel profile's "сохранил place" claims are
    # backed by these, not by file edits — surfaced by the FP run).
    "mcp__channel-intel__upsert_place",
    "mcp__channel-intel__save_reference",
    "mcp__channel-intel__record_visit",
    "mcp__channel-intel__attach_images",
    "mcp__channel-intel__link_reference_to_place",
    # Coach per-user data tools (dv-3668). Added 2026-08-19: the coach stopped
    # writing its logs through Bash/Edit when these landed, so every honest
    # «Записала…» started reporting claim_no_write — 16 of 27 mismatches to
    # date were this false positive, all after 2026-08-15.
    "mcp__agent__log_food",
    "mcp__agent__log_sleep",
    "mcp__agent__log_train",
    "mcp__agent__log_activity",
    "mcp__agent__food_edit",
    "mcp__agent__train_edit",
    "mcp__agent__activity_edit",
    "mcp__agent__log_delete",
    "mcp__agent__product_save",
    "mcp__agent__product_delete",
    "mcp__agent__portion_save",
}

# Claim families (ru/en, case-insensitive). Perfective past + performative
# statements = hard claims. Future-tense promises («запишу вечером») are
# measure-only in v1 (owner decision D13): logged for telemetry, never
# informed — EXCEPT "I'll remember"/«запомню», which the task names
# explicitly as a hard claim.
#
# Present-tense/imperfective memory verbs («помню», "I remember …") are
# deliberately NOT in any family: "I remember you said X" is recall, not a
# write claim.
_CLAIM_FAMILIES: List[tuple[str, str, bool]] = [
    # (family, pattern, inform)
    (
        "save_ru",
        # Perfective past (записал/записала/записали), short participles
        # («Завтрак записан», «Энергия записана», «всё записано»), and the
        # performative «запомню». The FP run surfaced the participle forms —
        # they are the coach's dominant confirmation shape.
        r"\b(?:записал(?:а|и)?|записан(?:а|о|ы)?|сохранил(?:а|и)?"
        r"|сохранен(?:а|о|ы)?|сохранён|запомнил(?:а|и)?|запомню"
        r"|зафиксировал(?:а|и)?|зафиксирован(?:а|о|ы)?"
        r"|вн[её]с(?:ла|ли)?\s+в\s+(?:лог|дневник|память|список)"
        r"|добавил(?:а|и)?\s+в\s+(?:лог|дневник|память|список)"
        r"|отметил(?:а|и)?\s+в)\b",
        True,
    ),
    (
        "save_en",
        r"\b(?:i(?:['’]| wi)ll remember"
        r"|i(?:['’]|ha)ve\s+(?:saved|logged|recorded|noted|added|written)"
        r"|(?:saved|logged|added|recorded)\s+(?:it|this|that|to your)"
        r"|written to"
        r"|updated your\s+(?:log|file|memory))\b",
        True,
    ),
    (
        "forget",
        r"\b(?:забыл(?:а)?\s+(?:про|это|тот)"
        r"|удалил(?:а)?\s+из\s+памяти"
        r"|ст[её]р(?:ла)?\s+из\s+памяти"
        r"|убрал(?:а)?\s+из\s+памяти"
        r"|i(?:['’]|ha)ve forgotten"
        r"|forgotten\b"
        r"|removed .{0,20}from (?:my )?memory"
        r"|deleted .{0,20}from (?:my )?memory)",
        True,
    ),
    (
        "correct",
        r"\b(?:исправил(?:а)?\s+(?:запись|факт|в памяти)"
        r"|обновил(?:а)?\s+(?:запись|факт|память)"
        r"|updated (?:my|your) memory"
        r"|corrected .{0,20}(?:memory|record|entry))",
        True,
    ),
    (
        "future_ru",
        r"\b(?:запишу|сохраню|зафиксирую|отмечу|добавлю\s+в\s+"
        r"(?:лог|дневник|память|список))\b",
        False,  # measure-only (D13)
    ),
]

_COMPILED = [
    (family, re.compile(pattern, re.IGNORECASE), inform)
    for family, pattern, inform in _CLAIM_FAMILIES
]

# Second/third-person guard: a claim verb about someone ELSE is not a claim
# by the agent («…а ты запомнил обратное» — the msg-766 shape; "the tracker
# recorded it"). A match whose immediately preceding word is one of these is
# discarded.
_SUBJECT_GUARD = re.compile(
    r"(?:\b(?:ты|вы|он|она|оно|прибор|трекер|you|he|she|it|tracker)\s+\S{0,12}\s*)$",
    re.IGNORECASE,
)

# Negation guard: «я не записала», «ещё не сохранено», "haven't saved" are
# admissions or questions, not claims. Surfaced by the FP run (msg 719: «…а я
# не записала»). Checked over the few words directly before the match.
_NEGATION_GUARD = re.compile(
    r"(?:\b(?:не|ещё не|еще не|пока не|haven['’]?t|hasn['’]?t|not(?:\s+yet)?)"
    r"\s*\S{0,12}\s*)$",
    re.IGNORECASE,
)

# State-description guard: «вот что записано», «у меня записано о тебе»,
# «уже сохранены», «что было сохранено» describe EXISTING memory (often
# truthfully, e.g. answering "what do you remember") — they are not claims of
# a write performed this turn, and a same-turn tool check cannot validate
# them. Dominant FP class in the offline labelling run (7 of 22 flags).
_STATE_GUARD = re.compile(
    r"(?:\b(?:уже|было|что|вот|нас|меня|тебя|already|previously)\s*)$",
    re.IGNORECASE,
)

# Subordinate/conditional mood: «придётся напомнить, чтобы я это записал» is
# a request for input, not a claim (FP run msg 428).
_SUBORDINATE_GUARD = re.compile(r"\bчтобы\b[^.!?\n]{0,40}$", re.IGNORECASE)

# Quoted/meta speech: the devops profile quoting «запомнил» while discussing
# this very failure class (FP run msg 328).
_QUOTE_GUARD = re.compile(r"[«\"'‘’]\s*$")

# Gym jargon: «путь движения зафиксирован (тренажёром)» — the machine fixes
# the bar path; nothing was written (FP run msgs 551/552). Pre-side: «путь …
# зафиксирован»; post-side: instrumental agent right after the verb.
_PATH_GUARD = re.compile(r"\bпуть\b[^.!?\n]{0,30}$", re.IGNORECASE)
_POST_AGENT_GUARD = re.compile(
    r"^\s*(?:тренаж|прибор|трекер|машин|систем)", re.IGNORECASE
)


@dataclass
class ClaimMatch:
    family: str
    matched_text: str
    excerpt: str  # +/-100 chars around the match
    inform: bool  # False = measure-only family (D13)


@dataclass
class TurnCheck:
    """Result of checking one turn. ``mismatch_kind`` is None when honest."""

    claim: Optional[ClaimMatch]
    satisfied_by: Optional[str]  # tool name, "Bash", "force_eval", or None
    mismatch_kind: Optional[str]  # 'claim_no_write' | 'trigger_no_write' | None


def detect_write_claim(text: Optional[str]) -> Optional[ClaimMatch]:
    """First write-claim match in a reply text, or None.

    Runs on the agent's response only — never on prompts (a quoted reply
    inside a prompt must not look like the agent claiming a write).
    """
    if not text:
        return None
    for family, regex, inform in _COMPILED:
        for m in regex.finditer(text):
            prefix = text[: m.start()]
            if (
                _SUBJECT_GUARD.search(prefix)
                or _NEGATION_GUARD.search(prefix)
                or _STATE_GUARD.search(prefix)
                or _SUBORDINATE_GUARD.search(prefix)
                or _QUOTE_GUARD.search(prefix)
                or _PATH_GUARD.search(prefix)
                or _POST_AGENT_GUARD.search(text[m.end() :])
            ):
                continue
            lo = max(0, m.start() - 100)
            return ClaimMatch(
                family=family,
                matched_text=m.group(0),
                excerpt=text[lo : m.end() + 100],
                inform=inform,
            )
    return None


def satisfied_write_tool(tools_used: Iterable[str]) -> Optional[str]:
    """The tool that satisfies a write claim this turn, if any.

    Prefers a specific write tool over ``Bash`` so ``satisfied_by="Bash"``
    keeps meaning "ONLY a shell call backed this claim" (D12).
    """
    used = [t for t in tools_used if t in WRITE_TOOLS]
    for tool in used:
        if tool != "Bash":
            return tool
    return "Bash" if used else None


def check_turn(
    response_text: Optional[str],
    tools_used: Iterable[str],
    trigger_fired: bool = False,
    force_eval_scheduled: bool = False,
) -> TurnCheck:
    """Classify one finished turn.

    Branch 1 (claim_no_write): the reply claims a durable write and no
    write-ish tool ran.
    Branch 2 (trigger_no_write): the user explicitly asked to remember
    («запомни»), no write-ish tool ran, and no forced evaluator run was
    scheduled — the force-eval path IS the guaranteed write, recorded as
    ``satisfied_by="force_eval"``.
    """
    claim = detect_write_claim(response_text)
    satisfied = satisfied_write_tool(tools_used)

    if satisfied is None and trigger_fired and force_eval_scheduled:
        satisfied = "force_eval"

    mismatch: Optional[str] = None
    if claim is not None and claim.inform and satisfied is None:
        mismatch = "claim_no_write"
    elif trigger_fired and satisfied is None:
        mismatch = "trigger_no_write"

    return TurnCheck(claim=claim, satisfied_by=satisfied, mismatch_kind=mismatch)
