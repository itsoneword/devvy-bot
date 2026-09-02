"""Reconcile extracted candidate facts against the saved fact set (dv-c5d9).

Second model call in the evaluator pipeline (owner decision D4): extraction
emits candidates; this component asks, per candidate, *does this contradict
anything already saved?* — never "is it better" — and deterministic code maps
the verdicts to dv-8254 ops (review R5: the reconciler is the only producer
of supersede/flag_conflict; ``invalidate`` stays an extraction observation
because "became false, nothing replaces it" has no candidate to judge).

The verdict validator is deliberately fact-preserving: any malformed or
missing verdict degrades to "keep the candidate" (add), never to dropping a
fact the extraction found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Set

import structlog

from .prompts import build_reconcile_prompt

logger = structlog.get_logger()

VALID_VERDICTS = {"new", "duplicate", "contradicts", "unsure"}

# dv-b9c7: a candidate that REVOKES a restriction (its lift/relaxation) must
# supersede the imposing fact even when safety-flagged — a constraint and its
# own revocation are exactly the case where keep-both is wrong. The lexicon is
# a deterministic backstop that only tightens verdicts the model already
# called contradicts+safety; it never invents an existing_id.
REVOCATION_LEXICON = (
    "lifted",
    "no longer",
    "снят",  # prefix: снят/снята/сняты/снято
    "прошло",
    "отменен",  # prefix: отменен/отменено/отменена
    "разрешено снова",
)


def matches_revocation_lexicon(content: str) -> bool:
    """True when the candidate text contains a release/lift phrase."""
    lowered = content.lower()
    return any(term in lowered for term in REVOCATION_LEXICON)


@dataclass
class Verdict:
    """One reconciler decision about one candidate, by index."""

    index: int
    verdict: str  # 'new' | 'duplicate' | 'contradicts' | 'unsure'
    existing_id: Optional[int] = None
    safety: bool = False
    revokes: bool = False


@dataclass
class MappedOps:
    """Deterministic verdict->op mapping output (the ONLY producer of
    supersede ops in the pipeline, review R5)."""

    ops: List[Dict[str, object]] = field(default_factory=list)
    # (existing fact id, index into ``ops``) pairs to flag as safety
    # conflicts once the add lands and its new row id is known.
    safety_flags: List[tuple[int, int]] = field(default_factory=list)
    # Candidate indexes dropped as already saved, with the matched fact id.
    already_saved: Dict[int, int] = field(default_factory=dict)


def validate_reconciler_output(raw_json: str, n_candidates: int) -> List[Verdict]:
    """Parse reconciler JSON into one Verdict per candidate.

    Defensive by construction: a candidate with no (usable) verdict gets
    "new" — reconciler sloppiness must lose an op decision, never a fact.
    A "duplicate"/"contradicts" without an integer ``existing_id`` degrades
    to "unsure" (keep both) for the same reason.
    """
    cleaned = raw_json.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(cleaned)
    if isinstance(data, dict) and "verdicts" in data:
        data = data["verdicts"]
    if not isinstance(data, list):
        raise ValueError("Expected JSON array of verdicts")

    by_index: Dict[int, Verdict] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or not 0 <= index < n_candidates:
            logger.warning("Reconciler verdict with bad index", entry=entry)
            continue
        verdict = entry.get("verdict")
        if verdict not in VALID_VERDICTS:
            logger.warning("Reconciler verdict unknown, degrading", entry=entry)
            verdict = "unsure"
        existing_id = entry.get("existing_id")
        if not isinstance(existing_id, int):
            existing_id = None
        if verdict in ("duplicate", "contradicts") and existing_id is None:
            logger.warning("Reconciler verdict without existing_id", entry=entry)
            verdict = "unsure"
        by_index[index] = Verdict(
            index=index,
            verdict=str(verdict),
            existing_id=existing_id,
            safety=bool(entry.get("safety")),
            revokes=bool(entry.get("revokes")),
        )
    return [
        by_index.get(i, Verdict(index=i, verdict="new")) for i in range(n_candidates)
    ]


def map_verdicts(
    candidates: List[Dict[str, object]],
    verdicts: List[Verdict],
    active_ids_by_scope: Dict[str, Set[int]],
) -> MappedOps:
    """Map verdicts to ops — deterministic code, not model output.

    ``active_ids_by_scope`` maps the candidate's scope value ("private" /
    "family") to the ids active in that scope; a verdict referencing an id
    outside the candidate's own scope is degraded to keep-both (the fixed
    guardrail: scope difference is not contradiction, and flag_conflict /
    supersede must pair rows inside one scope).
    """
    mapped = MappedOps()
    for cand, verdict in zip(candidates, verdicts):
        scope = str(cand.get("scope") or "private")
        in_scope = verdict.existing_id in active_ids_by_scope.get(scope, set())
        add_op = {k: v for k, v in cand.items() if k != "supersedes_id"}
        add_op["op"] = "add"

        if verdict.verdict == "duplicate" and in_scope:
            mapped.already_saved[verdict.index] = int(verdict.existing_id or 0)
            continue
        if verdict.verdict == "contradicts" and in_scope:
            existing_id = int(verdict.existing_id or 0)
            revokes = verdict.revokes
            if not revokes and verdict.safety:
                # Deterministic backstop: the model called it a safety
                # contradiction but omitted "revokes" — if the candidate
                # reads as a release ("сняты", "no longer"), keep-both
                # would pin a constraint next to its own cancellation.
                if matches_revocation_lexicon(str(cand.get("content") or "")):
                    revokes = True
                    logger.info(
                        "Revocation lexicon backstop fired",
                        existing_id=existing_id,
                        content=cand.get("content"),
                    )
            if verdict.safety and not revokes:
                # Keep both, flag the pair once the add's row id is known.
                mapped.safety_flags.append((existing_id, len(mapped.ops)))
                mapped.ops.append(add_op)
            else:
                # Non-safety contradiction, or a revocation: the revocation
                # branch takes precedence over keep-both (dv-b9c7).
                supersede = dict(add_op)
                supersede["op"] = "supersede"
                supersede["supersedes_id"] = existing_id
                mapped.ops.append(supersede)
            continue
        if verdict.verdict in ("duplicate", "contradicts"):
            logger.warning(
                "Reconciler verdict referenced out-of-scope id, keeping both",
                verdict=verdict.verdict,
                existing_id=verdict.existing_id,
                scope=scope,
            )
        # 'new', 'unsure', and every degraded case: keep the candidate.
        mapped.ops.append(add_op)
    return mapped


class FactReconciler:
    """Runs the reconcile model call. The CLI invocation is injected so the
    replay harness can reuse this class as the model-gate's judge with a
    different transport (dv-c5d9: "build step 3 first and reuse it")."""

    def __init__(
        self,
        invoke: Callable[[str], Awaitable[str]],
    ) -> None:
        self._invoke = invoke

    async def reconcile(
        self,
        candidates: List[Dict[str, object]],
        current_facts: str,
        family_facts: Optional[str] = None,
    ) -> List[Verdict]:
        """One batched call: all candidates against the full active fact set.

        Raises on transport/parse failure — the caller decides the fallback
        (apply candidates with their hints, v1 behaviour) so a reconciler
        outage can never drop extracted facts.
        """
        prompt = build_reconcile_prompt(candidates, current_facts, family_facts)
        raw = await self._invoke(prompt)
        return validate_reconciler_output(raw, len(candidates))
