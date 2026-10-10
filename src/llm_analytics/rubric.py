"""Tier-2 answer-scoring rubric (dv-300d).

The fixed criteria an LLM judge scores one turn against, kept in ONE file so
a rubric change has exactly one place to happen: bump ``RUBRIC_VERSION``.
Scores computed under different versions must never be averaged together —
``answer_scores.rubric_version`` is part of the storage key precisely so a
version bump re-scores instead of silently blending old and new numbers.

Score unit: one turn (the user message that produced ``response``, plus the
preceding turns as context) scored 1-5 on each of ``GENERIC_CRITERIA``, plus
a per-profile overlay (currently just ``chef``, from dv-5d5f) when
applicable. ``total`` is the sum of the five generic scores only (1-25) —
the overlay is deliberately excluded from ``total`` so scores stay
comparable across profiles that don't have one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

RUBRIC_VERSION = 1

GENERIC_CRITERIA: tuple[str, ...] = (
    "addresses_the_ask",
    "grounding",
    "calibration",
    "constraint_compliance",
    "actionability",
)

# Per-profile overlay criteria, keyed by profile slug. From dv-5d5f's three
# concrete failure modes: no canonical anchor before adapting a named dish,
# no search before writing a recipe out, and a "correction" issued while the
# recipe had scrolled out of context.
PROFILE_OVERLAY_CRITERIA: dict[str, tuple[str, ...]] = {
    "chef": (
        "chef_anchor_named",
        "chef_search_performed",
        "chef_no_blind_correction",
    ),
}

_CRITERIA_TEXT: dict[str, str] = {
    "addresses_the_ask": (
        "Does the response answer the question actually asked, not an "
        "adjacent or easier one? 5 = answers exactly what was asked. "
        "1 = answers a different question, or ignores part of a "
        "multi-part ask."
    ),
    "grounding": (
        "Are the load-bearing claims (quantities, ratios, technique, food "
        "safety, prices, facts about a named/branded thing) verified or "
        "independently verifiable, or are they invented/recalled from "
        "the model's blurry average of similar cases? If the answer "
        "depended on an external fact, was a search actually performed "
        "(check the tool-call context given to you) before stating it? "
        "5 = load-bearing claims are checked or self-evidently safe to "
        "state from general knowledge. 1 = a specific, checkable claim "
        "(e.g. '400g can of tomatoes', 'dissolve starch in hot liquid') "
        "is stated as fact with no verification and no hedge, and it "
        "matters if it's wrong."
    ),
    "calibration": (
        "Is a guess presented as a guess, or in the voice of a settled "
        "fact? This is independent of whether the guess is actually "
        "correct — score the HONESTY of the confidence, not the "
        "correctness of the content. 5 = uncertain things are flagged as "
        "uncertain ('I think', 'roughly', 'checked X, but Y is my "
        "estimate'); confident things are stated plainly because they "
        "really are safe bets. 1 = a claim invented or guessed from "
        "memory is delivered with the same flat, confident voice as a "
        "verified fact, with no hedge anywhere — the reader cannot tell "
        "the assistant is guessing."
    ),
    "constraint_compliance": (
        "Does the response respect what the user stated in the message "
        "itself — pantry/ingredients on hand, allergies, time budget, "
        "requested format, explicit 'don't do X'? 5 = every stated "
        "constraint is honored, and a constraint the response can't "
        "satisfy is called out rather than silently dropped. 1 = a "
        "stated constraint is silently ignored or overridden (e.g. the "
        "user says they barely have an ingredient and the response "
        "treats that ingredient as if they have a full supply)."
    ),
    "actionability": (
        "Can the user act on this immediately, without a clarifying "
        "round-trip? 5 = complete, usable as-is (right units, no missing "
        "step). 1 = user must come back and ask again before they can do "
        "anything, or the response is too vague/incomplete to act on."
    ),
    "chef_anchor_named": (
        "CHEF OVERLAY. If this turn adapts or gives a named dish (e.g. "
        "'shakshuka', 'carbonara', 'risotto'), does it name that dish's "
        "canonical core ingredient and roughly how much, before adapting "
        "it to the user's pantry — and say plainly if the pantry can't "
        "supply that core, rather than quietly substituting? 5 = core "
        "named and quantified, substitutions labelled as substitutions. "
        "1 = a named dish is delivered built around a substitute "
        "(e.g. tomato paste standing in for a can of tomatoes) with no "
        "mention that the base changed. If no named dish is being "
        "adapted in this turn, score 5 (not applicable, nothing to "
        "penalize)."
    ),
    "chef_search_performed": (
        "CHEF OVERLAY. If this turn writes out a full recipe for a named "
        "dish, or the user pushed back on a quantity/ratio, or the claim "
        "is about food chemistry/safety — was a web search actually "
        "performed for this turn (see the tool-call context) before "
        "answering? 5 = search performed where the persona's rules call "
        "for one, or no trigger applies in this turn. 1 = a trigger "
        "clearly applies (full recipe for a named dish, or pushback on "
        "quantities) and no search happened for this turn."
    ),
    "chef_no_blind_correction": (
        "CHEF OVERLAY. If this turn revises or corrects a recipe/ratio "
        "from earlier in the conversation, is the original actually "
        "visible in the context given to you? 5 = the correction quotes "
        "or clearly reflects the original, or no correction is being "
        "made this turn. 1 = the response corrects a quantity/ratio "
        "from a recipe that is NOT present in the given context — i.e. "
        "it is patching from memory rather than the real prior text."
    ),
}


def criteria_for(profile: Optional[str]) -> tuple[str, ...]:
    """Ordered criterion keys the judge scores for this profile."""
    overlay = PROFILE_OVERLAY_CRITERIA.get(profile or "", ())
    return GENERIC_CRITERIA + overlay


@dataclass
class ContextTurn:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class ParsedScore:
    scores: dict[
        str, dict[str, object]
    ]  # criterion -> {"score": int, "rationale": str}
    total: float
    summary: str
    flags: list[str] = field(default_factory=list)


_RATIONALE_CAP = 300


def _cap(text: str, limit: int = _RATIONALE_CAP) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_judge_prompt(
    profile: Optional[str],
    current_prompt: str,
    current_response: str,
    context_turns: list[ContextTurn],
    tool_calls_this_turn: list[str],
) -> str:
    """The full stdin prompt for one scoring call.

    ``tool_calls_this_turn`` is the list of tool names (e.g. 'WebSearch')
    actually invoked while producing ``current_response`` — this is what
    lets the judge answer "was a search done" instead of guessing from
    prose, and is the single most load-bearing input for the grounding
    and chef_search_performed criteria.
    """
    criteria = criteria_for(profile)
    criteria_block = "\n".join(
        f"- **{name}**: {_CRITERIA_TEXT[name]}" for name in criteria
    )

    context_block = "(no prior turns in this window)"
    if context_turns:
        lines = []
        for turn in context_turns:
            lines.append(f"[{turn.role}] {turn.text}")
        context_block = "\n\n".join(lines)

    tools_block = ", ".join(tool_calls_this_turn) if tool_calls_this_turn else "(none)"

    schema_keys = ",\n".join(
        f'    "{name}": {{"score": <1-5 int>, "rationale": "<=300 chars"}}'
        for name in criteria
    )

    return f"""You are scoring one turn of a conversation between a user and an AI \
assistant, on fixed criteria. Score honestly and independently — do not be \
lenient because the assistant "meant well"; a confident wrong answer is \
worse than an honest "I'm not sure".

Profile: {profile or "(none)"}

## Criteria

{criteria_block}

## Prior turns in this conversation (context only, do not score these)

{context_block}

## Turn being scored

[user] {current_prompt}

[assistant] {current_response}

## Tool calls made while producing this response

{tools_block}

## Output

Reply with ONLY a single JSON object, no prose before or after, no markdown \
fences, exactly this shape. Every rationale and the summary MUST be valid \
JSON string content: one line, no literal line breaks, and any quote marks \
you need inside the text written as ' (single quote) rather than " so the \
JSON stays parseable — this is strictly enforced and unparseable output is \
discarded:

{{
  "criteria": {{
{schema_keys}
  }},
  "summary": "<=300 char overall one-line rationale for the score>",
  "flags": ["optional short tags, e.g. blind_correction, invented_quantity"]
}}
"""


def _close_unbalanced(text: str) -> Optional[str]:
    """Append the closers an otherwise-complete JSON object is missing.

    Observed 2026-09-19 on the opus judge, reproducibly on the same turn: it
    scores every criterion correctly, then forgets the ``}`` that closes the
    ``criteria`` object and continues with ``"summary"`` at the criteria's
    nesting level. The payload is complete; only the envelope is broken. We
    close the open containers at EOF rather than guess where the brace
    belonged — which leaves ``summary``/``flags`` nested one level too deep,
    and ``parse_judge_output`` looks for them there as a fallback.

    Returns ``None`` if the text is broken in any other way (unterminated
    string, nothing open at EOF) — those are real failures, not this one.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or stack[-1] != ("{" if ch == "}" else "["):
                return None
            stack.pop()
    if in_string or not stack:
        return None
    return text + "".join("}" if c == "{" else "]" for c in reversed(stack))


@dataclass
class BatchTurn:
    """One turn in a batched scoring request."""

    message_id: int
    prompt: str
    response: str
    context_turns: list[ContextTurn]
    tool_calls: list[str]


def build_batch_judge_prompt(
    profile: Optional[str],
    turns: list[BatchTurn],
) -> str:
    """One stdin prompt scoring several turns in a single judge call.

    The criteria text, the scale and the anti-leniency framing are byte-identical
    to the single-turn prompt — the only difference is that N turns share one
    copy of the preamble. That is the whole point: the preamble is ~5.5k chars
    against ~1-3k chars of actual turn content, so it dominates the input.

    Scores from this path are written under a DIFFERENT judge_model label than
    the single-turn path, never mixed, because a batched judge can calibrate
    turns against each other and a solo judge cannot. Whether that shift is
    real is an empirical question — the two labels exist so it can be measured
    per message_id instead of argued about.
    """
    criteria = criteria_for(profile)
    criteria_block = "\n".join(
        f"- **{name}**: {_CRITERIA_TEXT[name]}" for name in criteria
    )
    schema_keys = ",\n".join(
        f'        "{name}": {{"score": <1-5 int>, "rationale": "<=300 chars"}}'
        for name in criteria
    )

    blocks = []
    for turn in turns:
        context_block = "(no prior turns in this window)"
        if turn.context_turns:
            context_block = "\n\n".join(
                f"[{c.role}] {c.text}" for c in turn.context_turns
            )
        tools_block = ", ".join(turn.tool_calls) if turn.tool_calls else "(none)"
        blocks.append(
            f"""### TURN {turn.message_id}

Prior turns (context only, do not score these):
{context_block}

[user] {turn.prompt}

[assistant] {turn.response}

Tool calls made while producing this response: {tools_block}"""
        )
    turns_block = "\n\n---\n\n".join(blocks)
    ids = ", ".join(str(t.message_id) for t in turns)

    return f"""You are scoring {len(turns)} independent turns of conversations \
between a user and an AI assistant, on fixed criteria. Score honestly — do not \
be lenient because the assistant "meant well"; a confident wrong answer is \
worse than an honest "I'm not sure".

Score each turn ON ITS OWN MERITS against the criteria below. Do NOT grade on a \
curve, do NOT rank the turns against each other, and do NOT let a strong or weak \
neighbour shift a score: a turn that would score 4 alone scores 4 here even if \
every other turn in this batch is better. The turns are unrelated and arrive in \
arbitrary order.

Profile: {profile or "(none)"}

## Criteria

{criteria_block}

## Turns to score

{turns_block}

## Output

Reply with ONLY a single JSON object, no prose before or after, no markdown \
fences, exactly this shape, with one entry per turn and nothing else. Every \
rationale and summary MUST be valid JSON string content: one line, no literal \
line breaks, and any quote marks you need inside the text written as ' (single \
quote) rather than " so the JSON stays parseable — this is strictly enforced \
and unparseable output is discarded. Close every object you open.

{{
  "turns": [
    {{
      "message_id": <int, one of: {ids}>,
{schema_keys},
      "summary": "<=300 char overall one-line rationale",
      "flags": ["optional short tags, e.g. blind_correction, invented_quantity"]
    }}
  ]
}}

Return exactly {len(turns)} entries, one for each of: {ids}.
"""


def _parse_criteria(
    raw_criteria: dict, profile: Optional[str]
) -> tuple[dict[str, dict[str, object]], dict[str, int]]:
    expected = criteria_for(profile)
    scores: dict[str, dict[str, object]] = {}
    int_scores: dict[str, int] = {}
    for name in expected:
        entry = raw_criteria.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"criterion '{name}' missing or malformed")
        score = entry.get("score")
        if not isinstance(score, (int, float)) or not (1 <= score <= 5):
            raise ValueError(f"criterion '{name}' score out of range: {score!r}")
        rationale = str(entry.get("rationale") or "")
        int_scores[name] = int(score)
        scores[name] = {"score": int(score), "rationale": _cap(rationale)}
    return scores, int_scores


def _build_parsed(
    scores: dict[str, dict[str, object]],
    int_scores: dict[str, int],
    envelope: dict,
) -> ParsedScore:
    total = float(sum(int_scores[name] for name in GENERIC_CRITERIA))
    summary = _cap(str(envelope.get("summary") or ""))
    flags_raw = envelope.get("flags") or []
    if not isinstance(flags_raw, list):
        raise ValueError("'flags' must be a list")
    return ParsedScore(
        scores=scores,
        total=total,
        summary=summary,
        flags=[str(f) for f in flags_raw],
    )


def parse_batch_judge_output(
    raw: str, profile: Optional[str], expected_ids: list[int]
) -> dict[int, ParsedScore]:
    """Parse a batched reply into {message_id: ParsedScore}.

    Turns the judge omitted, duplicated or invented are dropped rather than
    guessed at; the caller re-scores whatever is missing. A partial batch is
    recoverable, a silently misattributed score is not.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as first:
        repaired = _close_unbalanced(text)
        if repaired is None:
            raise ValueError(f"judge output is not valid JSON: {first}") from first
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as e:
            raise ValueError(f"judge output is not valid JSON: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("turns"), list):
        raise ValueError("batch judge output missing top-level 'turns' list")

    wanted = set(expected_ids)
    out: dict[int, ParsedScore] = {}
    for entry in data["turns"]:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("message_id")
        if not isinstance(mid, int) or mid not in wanted or mid in out:
            continue
        # Criteria may be inlined at the entry level (the shape we ask for) or
        # nested under "criteria" if the judge falls back to the solo shape.
        source = entry["criteria"] if isinstance(entry.get("criteria"), dict) else entry
        try:
            scores, int_scores = _parse_criteria(source, profile)
            out[mid] = _build_parsed(scores, int_scores, entry)
        except ValueError:
            continue
    return out


def parse_judge_output(raw: str, profile: Optional[str]) -> ParsedScore:
    """Parse and validate the judge's JSON reply. Raises ValueError on any
    malformed or incomplete output — callers must not silently substitute
    defaults for a judge that didn't follow the schema."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as first:
        repaired = _close_unbalanced(text)
        if repaired is None:
            raise ValueError(f"judge output is not valid JSON: {first}") from first
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as e:
            raise ValueError(f"judge output is not valid JSON: {e}") from e

    if not isinstance(data, dict) or "criteria" not in data:
        raise ValueError("judge output missing top-level 'criteria' object")

    raw_criteria = data["criteria"]
    if not isinstance(raw_criteria, dict):
        raise ValueError("'criteria' must be an object")

    scores, int_scores = _parse_criteria(raw_criteria, profile)
    # After a _close_unbalanced repair these sit inside `criteria`, one level
    # too deep. Reading them from there is what keeps the repair lossless.
    envelope = data if "summary" in data or "flags" in data else raw_criteria
    return _build_parsed(scores, int_scores, envelope)
