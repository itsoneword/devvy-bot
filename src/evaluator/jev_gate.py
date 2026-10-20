"""Jev gate in front of the reconciler (dv-7e11 phase 3).

Per candidate, one ``router.decide()`` call asks Jev three typed questions:
how the candidate relates to the saved facts (duplicate / contradicts / new),
which of the top-k lexically closest saved facts it relates to, and whether
it reports a state change of one (yes/no). The gate accepts Jev's answer
alone only for confident ``new`` / ``duplicate`` with no likely state change;
every ``contradicts``, every low-confidence answer and every Jev failure is
escalated to the existing Haiku reconciler (one batch, escalated candidates
only), so safety / revocation handling stays where it is: only a contradicts
verdict reads those flags, and contradicts is never accepted here.

Jev limits that shape this module (shadow test, ticket dv-7e11): a full fact
set overflows its context at ~165 facts, so the top-k prefilter is
mandatory; it favours the FIRST Choice option, so "new" / "none" go last;
dates are plain text; the state stays minimal.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import structlog

from ..model_router import Choice, DecideRequest, ModelRouter, Noul
from .reconciler import Verdict

logger = structlog.get_logger()

RELATION_Q = "relation"
RELATED_Q = "related_fact"
STATE_CHANGE_Q = "state_change"
NONE_OPTION = "none"

# Wording that held up in the shadow test (scratchpad run_jev.py), reordered
# so "new" is last (Jev's first-option bias would otherwise inflate it).
RELATION_INSTRUCTIONS = (
    "A memory store holds saved facts (saved_facts). A new candidate fact "
    "(candidate) was extracted from a conversation. Compare the candidate with "
    "the saved facts, by contradiction, never by which is better. Choose one "
    "option."
)
RELATION_CRITERIA: Dict[str, Optional[str]] = {
    "duplicate": (
        "A saved fact already states the same information as the candidate. "
        "Restatements and translations count as duplicates."
    ),
    "contradicts": (
        "The candidate and a saved fact cannot both be true because the thing "
        "CHANGED: the candidate states the new state of the same thing. Also "
        "use this when the candidate says a restriction from a saved fact is "
        "lifted, relaxed, expired or no longer applies."
    ),
    "new": (
        "No saved fact states the same information, and no saved fact "
        "conflicts with the candidate. Also use this when saved facts only "
        "overlap in topic or scope (for example 'no nuts in desserts' vs "
        "'allergic to nuts': both can be true), or when the candidate only "
        "adds detail to a saved fact."
    ),
}
RELATED_INSTRUCTIONS = (
    "Which saved fact (by id) states the same information as the candidate, "
    "or conflicts with it? Choose none when no saved fact does."
)
# Labelled shadow run (dv-7e11): Jev's systematic miss is a state change
# (open -> resolved, running -> completed, newer snapshot / measured value)
# answered "new" at high confidence. This yes/no question catches it.
STATE_CHANGE_INSTRUCTIONS = (
    "The candidate reports an update, completion, resolution or newer value "
    "of something described in one of the saved facts."
)

# Concurrent Jev calls per run; the shadow test ran 6 without hitting 429s
# it could not retry.
MAX_CONCURRENCY = 6

# Reasons recorded in op_stats.reconcile_gate (phase 5 dashboard reads them).
ACCEPT_NEW = "new"
ACCEPT_DUPLICATE = "duplicate"
ACCEPT_NO_SAVED_FACTS = "no_saved_facts"
ESC_CONTRADICTS = "contradicts"
ESC_LOW_CONF = "low_confidence"
ESC_NO_CONF = "no_confidence"
ESC_RELATED_NONE = "related_none"
ESC_RELATED_LOW_CONF = "related_low_confidence"
ESC_JEV_ERROR = "jev_unavailable"
ESC_STATE_CHANGE = "state_change"


@dataclass(frozen=True)
class JevGateConfig:
    """Settings for the gate; None on the service = gate off."""

    # Defaults from the labelled re-run (ticket dv-7e11, 2026-10-17):
    # new >= 0.8 errs ~1.7% before the state-change check, 0.7% after;
    # duplicate had no reliable band (2 confident answers in 610), so it
    # always escalates (> 1.0); state_change > 0.55 escalates.
    min_conf_new: float = 0.8
    min_conf_duplicate: float = 1.01
    max_state_change: float = 0.55
    top_k: int = 15


@dataclass
class GateOutcome:
    """Per-run gate result, keyed by candidate index."""

    accepted: Dict[int, Verdict] = field(default_factory=dict)
    escalated: List[int] = field(default_factory=list)
    reasons: Dict[int, str] = field(default_factory=dict)
    confidence: Dict[int, Optional[float]] = field(default_factory=dict)
    jev_calls: int = 0
    jev_cost_usd: float = 0.0

    def stats(self) -> Dict[str, Any]:
        """The op_stats.reconcile_gate block."""
        accepted_by = Counter(self.reasons[i] for i in self.accepted)
        escalated_by = Counter(self.reasons[i] for i in self.escalated)
        return {
            "accepted": len(self.accepted),
            "escalated": len(self.escalated),
            "accepted_by": dict(accepted_by),
            "escalated_by": dict(escalated_by),
            "jev_calls": self.jev_calls,
            "jev_cost_usd": round(self.jev_cost_usd, 8),
        }


def _tokens(text: str) -> Set[str]:
    return set(re.findall(r"\w{4,}", text.lower()))


def prefilter(content: str, facts: Sequence[Any], k: int) -> List[Any]:
    """Top-k saved facts by word overlap with the candidate (length-damped),
    returned in id order. Same scoring as the shadow test's build."""
    cand = _tokens(content)

    def score(fact: Any) -> float:
        words = _tokens(str(fact.content))
        return -len(cand & words) / (1.0 + math.sqrt(len(words)))

    top = sorted(facts, key=score)[:k]
    return sorted(top, key=lambda f: int(f.id))


def _fact_date(fact: Any) -> str:
    for attr in ("valid_from", "recorded_at"):
        value = getattr(fact, attr, None)
        if value is not None and hasattr(value, "strftime"):
            return str(value.strftime("%Y-%m-%d"))
    return ""


def build_request(content: str, facts: Sequence[Any]) -> DecideRequest:
    """One decide request: relation + related_fact over ``facts``."""
    related: Dict[str, Optional[str]] = {str(f.id): str(f.content) for f in facts}
    related[NONE_OPTION] = None  # last: first-option bias
    return DecideRequest(
        purpose="reconcile",
        state={
            "candidate": content,
            "saved_facts": [
                {
                    "id": int(f.id),
                    "kind": str(getattr(f, "kind", "user_fact")),
                    "date": _fact_date(f),
                    "text": str(f.content),
                }
                for f in facts
            ],
        },
        questions={
            RELATION_Q: Choice(
                instructions=RELATION_INSTRUCTIONS, criteria=RELATION_CRITERIA
            ),
            RELATED_Q: Choice(instructions=RELATED_INSTRUCTIONS, criteria=related),
            STATE_CHANGE_Q: Noul(instructions=STATE_CHANGE_INSTRUCTIONS),
        },
        # The gate escalates on its own; a per-candidate LLM fallback would
        # cost one haiku call per candidate and still give no confidence.
        fallback_model=None,
    )


def judge(
    index: int,
    answers: Mapping[str, Any],
    config: JevGateConfig,
) -> Tuple[Optional[Verdict], str, Optional[float]]:
    """Accept/escalate one candidate from its typed answers.

    Returns (verdict or None = escalate, reason, relation confidence).
    """
    relation = answers[RELATION_Q]
    related = answers[RELATED_Q]
    label = str(relation.value)
    conf = relation.confidence
    if label == "contradicts":
        return None, ESC_CONTRADICTS, conf
    if conf is None:
        return None, ESC_NO_CONF, conf
    # Checked before accepting new OR duplicate: a missed state change
    # either keeps a stale fact (new) or drops the update (duplicate).
    state_change = answers.get(STATE_CHANGE_Q)
    if state_change is not None and float(state_change.value) > config.max_state_change:
        return None, ESC_STATE_CHANGE, conf
    if label == "new":
        if conf >= config.min_conf_new:
            return Verdict(index=index, verdict="new"), ACCEPT_NEW, conf
        return None, ESC_LOW_CONF, conf
    # duplicate
    if conf < config.min_conf_duplicate:
        return None, ESC_LOW_CONF, conf
    if str(related.value) == NONE_OPTION:
        return None, ESC_RELATED_NONE, conf
    if related.confidence is None or related.confidence < config.min_conf_duplicate:
        return None, ESC_RELATED_LOW_CONF, conf
    verdict = Verdict(
        index=index, verdict="duplicate", existing_id=int(str(related.value))
    )
    return verdict, ACCEPT_DUPLICATE, conf


async def run_gate(
    router: ModelRouter,
    candidates: Sequence[Mapping[str, Any]],
    facts_by_scope: Mapping[str, Sequence[Any]],
    config: JevGateConfig,
) -> GateOutcome:
    """Run the gate over every candidate concurrently.

    A candidate is compared only with saved facts in its own scope: the
    verdict mapper degrades any cross-scope duplicate/contradiction to
    keep-both anyway, so out-of-scope facts could only add noise.
    """
    outcome = GateOutcome()
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(index: int, cand: Mapping[str, Any]) -> None:
        scope = str(cand.get("scope") or "private")
        content = str(cand.get("content") or "")
        facts = prefilter(content, facts_by_scope.get(scope, ()), config.top_k)
        if not facts:
            # Nothing saved to duplicate or contradict: new, no call needed.
            outcome.accepted[index] = Verdict(index=index, verdict="new")
            outcome.reasons[index] = ACCEPT_NO_SAVED_FACTS
            outcome.confidence[index] = None
            return
        try:
            async with sem:
                result = await router.decide(build_request(content, facts))
        except Exception as exc:
            logger.warning("Jev gate: decide failed, escalating", error=str(exc))
            outcome.escalated.append(index)
            outcome.reasons[index] = ESC_JEV_ERROR
            outcome.confidence[index] = None
            return
        outcome.jev_calls += 1
        outcome.jev_cost_usd += result.cost_usd or 0.0
        verdict, reason, conf = judge(index, result.answers, config)
        outcome.reasons[index] = reason
        outcome.confidence[index] = conf
        if verdict is None:
            outcome.escalated.append(index)
        else:
            outcome.accepted[index] = verdict

    await asyncio.gather(*(one(i, c) for i, c in enumerate(candidates)))
    outcome.escalated.sort()
    return outcome
