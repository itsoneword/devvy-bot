"""Jev screen in front of memory extraction (dv-7e11 step c).

Jev is a targeted-fact selector, not a skip gate (step b: 2% skip rate, 98%
recall of touched facts in the sent set). Per chunk of stored conversation it
answers, in parallel calls through ``router.decide()``:

* the content gate questions (``screen_spec.json``): logged, never gating;
* per saved fact two Nouls, ``touched`` ("does the conversation change,
  correct, cancel or add a detail to it?") and ``mentioned`` (adds repeats).

:func:`select_facts` turns the scores into the list Sonnet sees: facts scoring
>= ``fact_send_min`` on either question (top ``fact_send_max``), plus every
pinned fact, plus facts the ``remember`` tool saved in the slice.

Everything here is pure or a single router call; the extraction itself stays
in ``EvaluatorService``. Questions are written by humans once (the spec) and
filled by string substitution: no LLM builds a question.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..model_router import DecideRequest, ModelRouter, Noul

SPEC: Dict[str, Any] = json.loads(
    (Path(__file__).with_name("screen_spec.json")).read_text(encoding="utf-8")
)

TOUCHED = "fact_touched_short"
MENTIONED = "fact_mentioned"
TOUCHED_PREFIX = "t"
MENTIONED_PREFIX = "m"

# Jev call timeout: p50 0.4 s, one 5.5 s outlier in 17 calls (step a ledger);
# big packed calls are slower, so leave room.
JEV_TIMEOUT_SECONDS = 30.0
# Per-run ceiling on chunks screened in shadow (Sonnet runs once per chunk).
MAX_CHUNKS_PER_RUN = 6

_CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_TURN_SPLIT = re.compile(r"\n\n(?=(?:User|Assistant): )")


@dataclass(frozen=True)
class ScreenConfig:
    """``mode``: off | shadow | on. ``on`` is reserved for the cutover step
    and behaves as shadow until then (see EvaluatorService)."""

    mode: str = "off"
    chunk_tokens: int = 12_000
    fact_send_min: float = 0.3
    fact_send_max: int = 40

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "on")


Turn = Dict[str, str]


# ---------------------------------------------------------------- chunks


def est_tokens(obj: Any) -> int:
    """Jev token estimate: Latin chars/3.9 + Cyrillic chars/1.2 (step a fit)."""
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    cyr = sum(1 for ch in s if "Ѐ" <= ch <= "ӿ")
    return int((len(s) - cyr) / 3.9 + cyr / 1.2)


def clean_text(text: str) -> str:
    """Collapse long code blocks, cap one message (70% head / 30% tail)."""
    max_lines = int(SPEC["state"]["code_block_max_lines"])
    max_chars = int(SPEC["state"]["max_message_chars"])

    def _fence(m: "re.Match[str]") -> str:
        n = m.group(1).count("\n")
        return m.group(0) if n <= max_lines else f"[code block, {n} lines omitted]"

    text = _CODE_FENCE.sub(_fence, text or "").strip()
    if len(text) > max_chars:
        head = int(max_chars * 0.7)
        text = (
            text[:head]
            + f" […{len(text) - max_chars} chars omitted…] "
            + text[-(max_chars - head) :]
        )
    return text


def parse_turns(conversation_text: str) -> List[Turn]:
    """The evaluator's ``User: ...\\n\\nAssistant: ...`` text as Jev turns."""
    turns: List[Turn] = []
    for part in _TURN_SPLIT.split(conversation_text.strip()):
        if part.startswith("User: "):
            role, body = "user", part[len("User: ") :]
        elif part.startswith("Assistant: "):
            role, body = "assistant", part[len("Assistant: ") :]
        else:  # text before the first marker (e.g. an "[earlier ...]" note)
            role, body = "user", part
        body = clean_text(body)
        if body:
            turns.append({"id": f"t{len(turns)}{role[0]}", "from": role, "text": body})
    return turns


def chunk_turns(turns: Sequence[Turn], target_tokens: int) -> List[List[Turn]]:
    """Greedy chunks of ~``target_tokens`` Jev tokens, cut before user turns."""
    chunks: List[List[Turn]] = []
    cur: List[Turn] = []
    size = 0
    for turn in turns:
        n = est_tokens(turn) + 4
        if cur and size + n > target_tokens and turn["from"] == "user":
            chunks.append(cur)
            cur, size = [], 0
        cur.append(turn)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(chunk: Sequence[Turn]) -> str:
    """A chunk in the evaluator prompt's own format (clipped text, as Jev saw it)."""
    return "\n\n".join(
        f"{'User' if t['from'] == 'user' else 'Assistant'}: {t['text']}" for t in chunk
    )


def agent_key(agent_id: str) -> str:
    """Spec entry for a scope id: ``coach#u1000001`` -> coach, ``f1:chef`` -> chef."""
    base = agent_id.split("#")[0].split(":")[-1]
    return base if base in SPEC["agents"] else "general"


def build_state(agent: str, chunk: Sequence[Turn]) -> Dict[str, Any]:
    role = SPEC["agents"].get(agent, SPEC["agents"]["general"])["role"]
    return {"assistant_role": role, "conversation": list(chunk)}


# ------------------------------------------------------------- questions


def content_questions(agent: str) -> Dict[str, Noul]:
    """Gate questions for the agent: shared core + its extra and examples."""
    spec = SPEC["agents"].get(agent, SPEC["agents"]["general"])
    out: Dict[str, Noul] = {}
    for qid, q in SPEC["core_questions"].items():
        criteria = dict(q["criteria"])
        example = spec.get("examples", {}).get(qid)
        if example:
            criteria["true"] += " Examples for this assistant: " + example
        out[qid] = Noul(instructions=q["instructions"], criteria=criteria)
    for qid, q in spec.get("extra_questions", {}).items():
        out[qid] = Noul(instructions=q["instructions"], criteria=q.get("criteria"))
    return out


def fact_questions(facts: Sequence[Any]) -> Dict[str, Noul]:
    """Two Nouls per saved fact, keyed ``t<id>`` (touched) and ``m<id>``."""
    out: Dict[str, Noul] = {}
    for fact in facts:
        for prefix, template in (
            (TOUCHED_PREFIX, TOUCHED),
            (MENTIONED_PREFIX, MENTIONED),
        ):
            tpl = SPEC["fact_question_templates"][template]
            out[f"{prefix}{fact.id}"] = Noul(
                instructions={
                    "saved_fact": str(fact.content),
                    "question": tpl["question"],
                },
                criteria=tpl.get("criteria"),
            )
    return out


def _question_tokens(q: Noul) -> int:
    return est_tokens({"i": q.instructions, "c": q.criteria}) + 6


def pack_calls(
    state: Dict[str, Any],
    gate: Dict[str, Noul],
    facts_q: Dict[str, Noul],
    limit: Optional[int] = None,
) -> List[Dict[str, Noul]]:
    """Spread the questions evenly over the fewest calls that fit Jev's
    per-request budget. Gate questions ride in call 0. Every call carries the
    full state (billed per call)."""
    limit = limit or int(SPEC["fit"]["per_request_budget_tokens"])
    room = max(1, limit - est_tokens(state))
    gate_size = sum(_question_tokens(q) for q in gate.values())
    sizes = {qid: _question_tokens(q) for qid, q in facts_q.items()}
    total = gate_size + sum(sizes.values())
    n = max(1, math.ceil(total / room))
    target = total / n
    calls: List[Dict[str, Noul]] = []
    cur: Dict[str, Noul] = dict(gate)
    size = gate_size
    for qid, q in facts_q.items():
        sz = sizes[qid]
        if cur and (size + sz > room or (size >= target and len(calls) < n - 1)):
            calls.append(cur)
            cur, size = {}, 0
        cur[qid] = q
        size += sz
    if cur:
        calls.append(cur)
    return calls


# ---------------------------------------------------------------- calling


@dataclass
class JevScores:
    answers: Dict[str, float] = field(default_factory=dict)
    calls: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    ms: int = 0

    def fact_scores(self, fact_id: int) -> Tuple[float, float]:
        return (
            self.answers.get(f"{TOUCHED_PREFIX}{fact_id}", 0.0),
            self.answers.get(f"{MENTIONED_PREFIX}{fact_id}", 0.0),
        )


async def score_chunk(
    router: ModelRouter,
    state: Dict[str, Any],
    calls: Sequence[Dict[str, Noul]],
) -> JevScores:
    """Run the packed calls concurrently through ``router.decide``.

    ``fallback_model=None``: a Jev failure raises ``ModelCallError``. The
    caller gives up on the chunk; no LLM ever answers 400 yes/no questions."""
    started = time.monotonic()
    results = await asyncio.gather(
        *(
            router.decide(
                DecideRequest(
                    purpose="screen",
                    state=state,
                    questions=questions,
                    timeout_seconds=JEV_TIMEOUT_SECONDS,
                    fallback_model=None,
                )
            )
            for questions in calls
        ),
        return_exceptions=True,
    )
    scores = JevScores(calls=len(calls))
    for res in results:
        if isinstance(res, BaseException):
            raise res
        scores.input_tokens += res.input_tokens
        scores.cost_usd += res.cost_usd or 0.0
        for qid, answer in res.answers.items():
            scores.answers[qid] = float(answer.value)
    scores.ms = int((time.monotonic() - started) * 1000)
    return scores


# ------------------------------------------------------------- selection


@dataclass
class Selection:
    """The facts one Sonnet call sees, and why each is there."""

    facts: List[Any]
    reasons: Dict[int, str]  # fact id -> "score" | "pinned" | "remember"
    over_threshold: int  # facts >= fact_send_min before the cap
    capped: bool

    @property
    def ids(self) -> Set[int]:
        return {f.id for f in self.facts}


def select_facts(
    facts: Sequence[Any],
    scores: JevScores,
    cfg: ScreenConfig,
    remember_ids: Optional[Set[int]] = None,
) -> Selection:
    """Score-selected (>= ``fact_send_min`` on either question, top
    ``fact_send_max``) plus all pinned plus ``remember``-tool facts."""
    remember_ids = remember_ids or set()
    scored = []
    for fact in facts:
        best = max(scores.fact_scores(fact.id))
        if best >= cfg.fact_send_min:
            scored.append((best, fact))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    over = len(scored)
    chosen = {f.id: "score" for _, f in scored[: cfg.fact_send_max]}
    for fact in facts:
        if fact.tier == "pinned":
            chosen[fact.id] = "pinned"
        elif fact.id in remember_ids:
            chosen.setdefault(fact.id, "remember")
    picked = sorted((f for f in facts if f.id in chosen), key=lambda f: f.id)
    return Selection(
        facts=picked,
        reasons={f.id: chosen[f.id] for f in picked},
        over_threshold=over,
        capped=over > cfg.fact_send_max,
    )


SELECTED_FACTS_HEADER = """\
NOTE: the list below is NOT the whole memory. It holds only the saved facts a
screen marked as possibly touched by this conversation, plus the pinned
facts. Other facts exist; do not assume a fact is absent because it is not
listed. Never use an id that is not listed.

Extra fields on an "add" op, only when they apply:
  "supersedes_id": <listed id> — the new statement replaces that fact.
  "revokes": true — with supersedes_id: the statement lifts, relaxes or ends a
      restriction that fact imposed (e.g. "peanuts are fine now").
  "conflicts_with_id": <listed id> — safety-relevant and cannot be settled:
      keep both facts (e.g. a second allergy, "allergic to walnuts").
Ticket and job status is NOT a fact (the Durability rules above apply in full):
emit nothing for "dv-xxxx is in progress / done / shipped", "commit ... landed",
"job started", "waiting for review". Tickets live on the board.
If a listed fact is a status the conversation shows has ended, emit
{"op": "invalidate", "target_id": <id>}. If a listed fact holds other
still-true statements besides the one that changed (or that you invalidate),
re-add each of those verbatim as a separate "add" op; an op that closes such a
fact without re-adding them is rejected.

"""


def chunk_date_note(start: Optional[datetime], end: Optional[datetime]) -> str:
    """The conversation's own date for the prompt (b2: 6/115 new ops carried
    the replay date because nothing told Sonnet when the chat happened).
    ``start``/``end`` are the slice's first/last message times."""
    a = start or end
    b = end or start
    if a is None or b is None:
        return ""
    d1, d2 = a.strftime("%d.%m.%y"), b.strftime("%d.%m.%y")
    span = d1 if d1 == d2 else f"{d1} - {d2}"
    return (
        f"CONVERSATION DATE: {span} (DD.MM.YY). This is when the conversation "
        "below took place. Any date you write into a fact must come from the "
        "conversation or from this date, never from today's date.\n\n"
    )


# ------------------------------------------------- compound-fact rule (b2)

_CLAIM_SPLIT = re.compile(r"[;\n]+|(?<=[.!?])\s+(?=[A-ZА-ЯЁ0-9\"(])")
_MIN_CLAIM_CHARS = 12
_NORM = re.compile(r"[\W_]+", re.U)


def _norm(text: str) -> str:
    return _NORM.sub(" ", text.lower()).strip()


def split_claims(text: str) -> List[str]:
    """Independent claims of a fact: split on ``;``, newlines and sentence
    ends; fragments under 12 chars are glued to the previous claim."""
    claims: List[str] = []
    for part in _CLAIM_SPLIT.split(text or ""):
        part = part.strip()
        if not part:
            continue
        if claims and len(part) < _MIN_CLAIM_CHARS:
            claims[-1] += " " + part
        else:
            claims.append(part)
    return claims


def lost_claims(target_text: str, added_texts: Sequence[str]) -> List[str]:
    """Claims of a compound fact that no added text carries (normalised
    substring match). A fact with one claim is never compound -> []."""
    claims = split_claims(target_text)
    if len(claims) < 2:
        return []
    pool = [_norm(t) for t in added_texts]
    return [c for c in claims if not any(_norm(c) in p for p in pool)]


def closes_compound_safely(target_text: str, added_texts: Sequence[str]) -> bool:
    """The rule (documented in docs/DECISIONS.md, dv-7e11 note 2026-10-17):
    an op closing a compound fact (>=2 claims) may lose at most ONE claim, the
    one that changed; every other claim must be re-added verbatim. Otherwise
    the op is rejected and the old fact stays active."""
    return len(lost_claims(target_text, added_texts)) <= 1
