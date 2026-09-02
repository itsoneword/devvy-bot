"""Apply validated evaluator memory ops through MemoryRepository."""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import structlog

from src.storage.memory_repository import MemoryRepository, truncate_at_word

logger = structlog.get_logger()

# A user_fact above this is coerced to kind='conversation_result' (dv-7517
# step 5): stored in full, injected only as a one-line pointer, retrieved via
# recall(). Rationale: durable statements average ~230 rendered chars; the
# live 2.6k-char analysis blob (fact ~399) alone ate 16% of the facts budget.
# 600 ≈ 2.6 rendered lines ≈ 5% of the 12k budget. Applies to every
# apply_ops caller — batch evaluator and the in-session remember tool alike.
MAX_INJECTABLE_FACT_CHARS = 600

# OpOutcome.outcome values that mean "the op changed the database".
_APPLIED_OUTCOMES = frozenset(
    {"added", "superseded", "invalidated", "conflict_flagged"}
)


@dataclass
class OpOutcome:
    """What apply_ops actually did with ONE op, in batch order.

    ``fact_id`` is the NEW row id for added/superseded ops (the dv-c5d9
    pipeline needs it to flag safety conflicts against rows it just wrote).
    """

    op: str
    outcome: str  # 'added'|'superseded'|'invalidated'|'conflict_flagged'|
    # 'duplicate'|'noop_supersede'|'not_found'|'error'
    fact_id: Optional[int] = None
    scope: str = ""


@dataclass
class ApplyResult:
    """Per-run breakdown of an apply_ops batch (dv-c5d9 decision 4).

    Truthy exactly when at least one op changed the database, so callers that
    used the old ``int`` return truthily keep working. Counters are split by
    reason so the user-facing report can reconcile exactly with the rows
    written (dv-c5d9 criterion 4).
    """

    outcomes: List[OpOutcome] = field(default_factory=list)

    def _count(self, *outcomes: str) -> int:
        return sum(1 for o in self.outcomes if o.outcome in outcomes)

    @property
    def applied(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome in _APPLIED_OUTCOMES)

    @property
    def added(self) -> int:
        return self._count("added")

    @property
    def superseded(self) -> int:
        return self._count("superseded")

    @property
    def invalidated(self) -> int:
        return self._count("invalidated")

    @property
    def conflicts_flagged(self) -> int:
        return self._count("conflict_flagged")

    @property
    def dropped_duplicate(self) -> int:
        return self._count("duplicate", "noop_supersede")

    def __bool__(self) -> bool:
        return self.applied > 0


# Two normalised contents this similar are treated as the same fact. 0.92 is
# tight enough that "likes spicy food" / "likes spicy foods" collapses while
# "allergic to peanuts" / "allergic to walnuts" (0.90) survives as distinct --
# the failure mode to avoid is silently dropping a real allergy.
DUPLICATE_RATIO = 0.92

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+", re.UNICODE)


def normalize_fact(content: str) -> str:
    """Case/punctuation/whitespace-insensitive form of a fact's content.

    NFKC first so visually identical text that differs in Unicode composition
    (a real risk with the mixed ru/en facts this bot stores) compares equal.
    """
    text = unicodedata.normalize("NFKC", content).casefold()
    return _WS.sub(" ", _PUNCT.sub(" ", text)).strip()


def is_duplicate(candidate: str, existing: str) -> bool:
    """True when two normalised fact contents say the same thing.

    Lexical only -- deliberately no embedding or LLM call, which would put a
    network round trip on every memory write. Cross-language restatements
    ("любит острое" vs "likes spicy food") are NOT caught here; if those show
    up in the rejection logs, that is the case for a semantic check.
    """
    if not candidate or not existing:
        return False
    if candidate == existing:
        return True
    # Cheap length prefilter -- SequenceMatcher can never reach the threshold
    # when the lengths are this far apart.
    shorter, longer = sorted((len(candidate), len(existing)))
    if longer and shorter / longer < DUPLICATE_RATIO:
        return False
    return SequenceMatcher(None, candidate, existing).ratio() >= DUPLICATE_RATIO


class MemoryWriter:
    """Applies validated evaluator ops to agent memory.

    Vocabulary (dv-8254): ``add``, ``supersede`` (metadata-only
    close+insert+link), ``invalidate`` (close only), ``flag_conflict``
    (both rows stay active, paired via ``conflict_group``).

    Duplicate suppression is enforced HERE, not in the evaluator prompt. The
    prompt has said "never restate facts already present unchanged" all along;
    a rule in a prompt is not a guarantee (see docs/FINDINGS.md 2026-08-04),
    so the same rule is applied structurally on the write path where it can be
    counted.
    """

    def __init__(self, repo: MemoryRepository, source: str = "evaluator") -> None:
        self._repo = repo
        # Stamped on every row this writer creates (``memory_facts.source``).
        # The batch evaluator uses the default; the in-session ``remember``
        # tool passes "agent_tool", which is what makes "how much memory did
        # the model capture itself vs. how much did the batch job find" a SQL
        # question rather than a guess (dv-9c24).
        self._source = source
        # Process-lifetime counters, read by /diag-style diagnostics. Rejections
        # are the signal for whether the model actually duplicates: the
        # duplicates observed on 2026-08-04 came from the concurrent-run race
        # (fixed in 0a8e68f / 6ec8a09), not necessarily from the model.
        self.duplicates_rejected = 0
        self.ops_applied = 0

    async def apply_ops(
        self,
        agent_id: str,
        ops: List[Dict[str, object]],
        family_agent_id: Optional[str] = None,
    ) -> ApplyResult:
        """Apply ops for one agent. Returns a per-op :class:`ApplyResult`.

        Ops carrying ``scope: "family"`` are written under
        ``family_agent_id`` (the shared ``f{family_id}:{slug}`` scope).
        Defensive default: without a family scope every op — whatever its
        claimed scope — lands in the private ``agent_id``.

        An ``add`` whose content duplicates a fact already active in the target
        scope, or an earlier add in this same batch, is dropped and counted
        rather than written.
        """
        result = ApplyResult()
        # Normalised content of every active fact per scope, loaded once and
        # then extended as this batch writes, so an add cannot duplicate a
        # sibling add from the same run either.
        seen: Dict[str, List[Tuple[Optional[int], str]]] = {}

        for op in ops:
            target = (
                family_agent_id
                if op.get("scope") == "family" and family_agent_id
                else agent_id
            )
            if target not in seen:
                seen[target] = await self._load_scope(target)
            op_name = str(op.get("op"))
            outcome = OpOutcome(op=op_name, outcome="error", scope=target)
            result.outcomes.append(outcome)

            try:
                # Inside the try with everything else: one malformed op must not
                # abort the ops that follow it.
                if op_name == "invalidate":
                    # Metadata-only close, no replacement. Carries no content
                    # by contract (the validator strips any) — the structural
                    # guarantee that nothing rewrites an existing row.
                    target_id = int(op["target_id"])  # type: ignore[call-overload]
                    if await self._repo.invalidate_fact(
                        target_id, target, source=self._source
                    ):
                        seen[target] = [e for e in seen[target] if e[0] != target_id]
                        outcome.outcome = "invalidated"
                        outcome.fact_id = target_id
                        self.ops_applied += 1
                    else:
                        outcome.outcome = "not_found"
                        logger.warning(
                            "invalidate matched no active fact",
                            agent_id=target,
                            target_id=target_id,
                        )
                    continue
                if op_name == "flag_conflict":
                    ids = op["ids"]
                    id_a, id_b = int(ids[0]), int(ids[1])  # type: ignore[index]
                    await self._repo.flag_conflict(id_a, id_b, target)
                    outcome.outcome = "conflict_flagged"
                    self.ops_applied += 1
                    continue

                content = str(op["content"])
                kind = str(op.get("kind") or "user_fact")
                label = op.get("label")
                source_ref = op.get("source_ref")
                tier_val = str(op.get("tier") or "general")
                if (
                    kind == "user_fact"
                    and tier_val == "general"
                    and len(content) > MAX_INJECTABLE_FACT_CHARS
                ):
                    # general only: a pinned/core fact must never silently
                    # lose its always-injected status to pointer-land.
                    # Oversized "facts" are results/analyses, not identity
                    # statements: keep the full text but inject it as a
                    # pointer only (label = first line when none given).
                    kind = "conversation_result"
                    if not label:
                        label = truncate_at_word(content.strip().splitlines()[0])
                    logger.info(
                        "Oversized user_fact coerced to conversation_result",
                        agent_id=target,
                        content_chars=len(content),
                        label=label,
                    )
                normalized = normalize_fact(content)
                if op_name == "add":
                    clash = self._find_duplicate(seen[target], normalized)
                    if clash is not None:
                        self.duplicates_rejected += 1
                        outcome.outcome = "duplicate"
                        outcome.fact_id = clash[0]
                        logger.info(
                            "Rejected duplicate memory add",
                            agent_id=target,
                            existing_id=clash[0],
                            existing=clash[1][:120],
                            candidate=normalized[:120],
                        )
                        continue
                    new_id = await self._repo.add_fact(
                        target,
                        str(op["tier"]),
                        content,
                        label=label,  # type: ignore[arg-type]
                        source=self._source,
                        kind=kind,
                        source_ref=source_ref,  # type: ignore[arg-type]
                    )
                    seen[target].append((new_id, normalized))
                    outcome.outcome = "added"
                    outcome.fact_id = new_id
                elif op_name == "supersede":
                    old_id = int(op["supersedes_id"])  # type: ignore[call-overload]
                    if self._is_noop_supersede(seen[target], old_id, normalized):
                        self.duplicates_rejected += 1
                        outcome.outcome = "noop_supersede"
                        outcome.fact_id = old_id
                        logger.info(
                            "Rejected no-op memory supersede",
                            agent_id=target,
                            supersedes_id=old_id,
                            candidate=normalized[:120],
                        )
                        continue
                    new_id = await self._repo.supersede_fact(
                        old_id,
                        target,
                        str(op["tier"]),
                        content,
                        label=label,  # type: ignore[arg-type]
                        source=self._source,
                        kind=kind,
                        source_ref=source_ref,  # type: ignore[arg-type]
                    )
                    # The old row is gone from the active set; the new content
                    # takes its place for the rest of this batch.
                    seen[target] = [e for e in seen[target] if e[0] != old_id]
                    seen[target].append((new_id, normalized))
                    outcome.outcome = "superseded"
                    outcome.fact_id = new_id
                self.ops_applied += 1
            except Exception as e:
                logger.error("Failed to apply memory op", op=op, error=str(e))

        if len(ops) != result.applied:
            logger.info(
                "Memory ops applied",
                agent_id=agent_id,
                emitted=len(ops),
                applied=result.applied,
                duplicates_rejected_total=self.duplicates_rejected,
            )
        return result

    async def _load_scope(self, agent_id: str) -> List[Tuple[Optional[int], str]]:
        """(id, normalised content) for every active fact in a scope."""
        try:
            facts = await self._repo.get_facts(agent_id)
        except Exception as e:
            # Never let a read failure block writes -- worst case a duplicate
            # slips through, which is what the old behaviour did every time.
            logger.warning(
                "Duplicate check unavailable, writing unchecked",
                agent_id=agent_id,
                error=str(e),
            )
            return []
        return [(f.id, normalize_fact(f.content)) for f in facts]

    @staticmethod
    def _find_duplicate(
        existing: List[Tuple[Optional[int], str]], normalized: str
    ) -> Optional[Tuple[Optional[int], str]]:
        for entry in existing:
            if is_duplicate(normalized, entry[1]):
                return entry
        return None

    @staticmethod
    def _is_noop_supersede(
        existing: List[Tuple[Optional[int], str]], old_id: int, normalized: str
    ) -> bool:
        """A supersede that replaces a fact with itself is pure churn.

        Only the superseded row is compared: superseding fact A with text that
        happens to resemble unrelated fact B is a legitimate merge, not a
        duplicate.
        """
        for fact_id, content in existing:
            if fact_id == old_id:
                return is_duplicate(normalized, content)
        return False
