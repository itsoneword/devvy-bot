"""Evaluator service: the extract -> reconcile -> report pipeline (dv-c5d9).

Invokes Claude/Codex CLI at each token checkpoint to emit candidate memory
facts, reconciles them against the scope's saved facts with a second (haiku)
call, maps verdicts to dv-8254 ops in deterministic code, applies them via
MemoryWriter, and reports the real per-run breakdown. The memory_extractions
audit row records the breakdown as ``op_stats`` JSON so every report is
checkable by query (criterion 4).
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

import structlog

from ..model_router import CallRequest, get_router
from ..profiles.registry import family_agent_id_for_agent
from ..storage.extraction_repository import MemoryExtractionRepository
from ..storage.repositories import MessageRepository
from .episodic_guard import classify as classify_episodic
from .episodic_guard import classify_ticket_status
from .jev_gate import GateOutcome, JevGateConfig, run_gate
from .prompts import build_evaluator_prompt, format_current_facts
from .reconciler import FactReconciler, MappedOps, Verdict, map_verdicts
from .screen import (
    MAX_CHUNKS_PER_RUN,
    SELECTED_FACTS_HEADER,
    ScreenConfig,
    agent_key,
    build_state,
    chunk_date_note,
    chunk_text,
    chunk_turns,
    closes_compound_safely,
    content_questions,
    fact_questions,
    pack_calls,
    parse_turns,
    score_chunk,
    select_facts,
)
from .validator import validate_evaluator_output
from .writer import ApplyResult, MemoryWriter

if TYPE_CHECKING:
    from ..storage.database import DatabaseManager
    from ..storage.memory_repository import MemoryRepository
    from ..token.tracker import TokenTracker

logger = structlog.get_logger()

# Replaces Claude Code's default system prompt on the extraction call. Kept
# deliberately thin: the real instructions live in the user-message prompt
# built by build_evaluator_prompt, and duplicating them here would only give
# the model two places to disagree with itself.
EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured memory ops from conversations. Follow the output "
    "format in the user message exactly and reply with JSON only."
)

# The extraction answer's contract: validate_evaluator_output's op list.
# Only ``op`` is required; the other keys are optional with their real types
# (an explicit null = absent there). Route-agnostic: the router wraps the
# array root and makes it strict for OpenAI-style structured outputs (the
# dv-0c01 rule), and drops null optional keys from the answer.
_OP_PROPERTIES: Dict[str, Any] = {
    "op": {
        "type": "string",
        "enum": ["add", "supersede", "invalidate", "flag_conflict"],
    },
    # Absent on the metadata-only ops (invalidate/flag_conflict).
    "tier": {"type": "string", "enum": ["pinned", "general"]},
    "content": {"type": "string"},
    "label": {"type": "string"},
    "supersedes_id": {"type": "integer"},
    "kind": {"type": "string", "enum": ["user_fact", "conversation_result"]},
    "source_ref": {"type": "string"},
    "target_id": {"type": "integer"},
    "ids": {"type": "array", "items": {"type": "integer"}},
    "scope": {"type": "string", "enum": ["private", "family"]},
    # Screen path (dv-7e11); absent on the classic path.
    "revokes": {"type": "boolean"},
    "conflicts_with_id": {"type": "integer"},
}
EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": _OP_PROPERTIES,
        "required": ["op"],
    },
}

RECONCILE_SYSTEM_PROMPT = (
    "You reconcile candidate facts against an agent's saved facts. Follow the "
    "output format in the user message exactly and reply with JSON only."
)


class EvaluatorService:
    """Extract and persist conversation memory ops via Claude/Codex CLI."""

    def __init__(
        self,
        memory_repo: "MemoryRepository",
        writer: MemoryWriter,
        token_tracker: "TokenTracker",
        db: "DatabaseManager",
        notify_callback: Optional[Callable] = None,
        max_delta_chars: int = 120_000,
        timeout_seconds: int = 120,
        reconcile_enabled: bool = True,
        episodic_guard_enabled: bool = True,
        jev_gate: Optional[JevGateConfig] = None,
        screen: Optional[ScreenConfig] = None,
    ) -> None:
        self._repo = memory_repo
        self._writer = writer
        self._token_tracker = token_tracker
        self._db = db
        self._extractions = MemoryExtractionRepository(db)
        self._messages = MessageRepository(db)
        self._notify = notify_callback  # async fn(user_id, message, agent_id)
        # Models: the extraction / screen_extraction / reconcile call routes
        # (models.yaml, dv-781f). Extraction follows the chat's backend.
        self._max_delta_chars = max_delta_chars
        # A timeout stops the CLI child (it used to leak) and surfaces as a
        # 'timeout ...' error_message so the rate is queryable (plan D6).
        self._timeout = timeout_seconds
        # Reconciler is a second, haiku-routed call (owner D4); haiku needs no
        # gate here — "a comparison between two short texts, mechanical".
        self._reconcile_enabled = reconcile_enabled
        # Deterministic episodic backstop (dv-7517 step 3). Kill switch:
        # EVALUATOR_EPISODIC_GUARD=false — kept for the first weeks in case
        # the keyword heuristic misfires on live RU/EN phrasing.
        self._episodic_guard_enabled = episodic_guard_enabled
        # Jev gate in front of the reconciler (dv-7e11 phase 3). None = off:
        # every candidate goes to the haiku reconciler, as before.
        self._jev_gate = jev_gate
        # Late-bound wrapper (not the bound method) so tests can patch
        # _invoke_reconcile_cli on the instance.
        self._reconciler = FactReconciler(
            invoke=lambda prompt: self._invoke_reconcile_cli(prompt)
        )
        # Serialize runs per memory scope. A turn that both crosses an eval
        # checkpoint AND rolls the window fires two fire-and-forget runs
        # seconds apart over overlapping conversation text; both read the
        # fact list before either writes, so both "add" the same facts
        # (observed 2026-08-04: every `...:N` / `...:refresh` extraction pair
        # produced duplicate rows). Holding a per-agent lock across
        # read -> CLI -> write means run #2 sees run #1's facts and the
        # prompt's own "don't re-add known facts" rule dedupes it.
        self._scope_locks: dict[str, asyncio.Lock] = {}
        # Jev screen (dv-7e11 step c). Off by default here; main.py passes the
        # MEMORY_SCREEN_* settings. Shadow runs are detached tasks: the classic
        # path never waits for them and never sees their errors.
        self._screen = screen or ScreenConfig()
        self._shadow_tasks: set[asyncio.Task] = set()

    async def run(
        self,
        session_id: str,
        user_id: int,
        conversation_text: str,
        cumulative_tokens: int,
        agent_id: str = "devops",
        backend: str = "claude",
        family_id: Optional[int] = None,
        since_message_id: Optional[int] = None,
    ) -> bool:
        """Run evaluator extraction for one agent. Returns True on success.

        Facts are scoped by ``agent_id`` (profile slug). The evaluator is shown
        the agent's current facts and emits add/supersede ops applied via the
        MemoryRepository.

        ``family_id`` (the speaking user's family, if any) enables family
        scope selection: the evaluator additionally sees the shared
        ``f{family_id}:{slug}`` facts and may emit ops with
        ``scope: "family"`` for explicitly family-framed facts. None = the
        prompt never mentions family scope and any stray family op is
        coerced private.

        ``since_message_id`` is the checkpoint boundary (the slice start):
        facts saved by the in-session ``remember`` tool after it count as
        "already saved" in the run report rather than dropped duplicates
        (dv-c5d9 step 1). None/0 = no lower bound.
        """
        logger.info(
            "Running evaluator",
            session_id=session_id,
            cumulative_tokens=cumulative_tokens,
            agent_id=agent_id,
            family_id=family_id,
        )

        lock = self._scope_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            return await self._run_locked(
                session_id=session_id,
                user_id=user_id,
                conversation_text=conversation_text,
                cumulative_tokens=cumulative_tokens,
                agent_id=agent_id,
                backend=backend,
                family_id=family_id,
                since_message_id=since_message_id,
            )

    async def _run_locked(
        self,
        session_id: str,
        user_id: int,
        conversation_text: str,
        cumulative_tokens: int,
        agent_id: str,
        backend: str,
        family_id: Optional[int],
        since_message_id: Optional[int] = None,
    ) -> bool:
        """Body of ``run``; caller holds the per-agent scope lock."""

        family_scope_id: Optional[str] = None
        family_facts_text: Optional[str] = None
        family_facts: List = []
        if family_id is not None:
            family_scope_id = family_agent_id_for_agent(agent_id, family_id)
            family_facts = await self._repo.get_facts(family_scope_id)
            family_facts_text = format_current_facts(family_facts)

        conversation_text = self._bound_delta(conversation_text, agent_id)

        facts = await self._repo.get_facts(agent_id)
        prompt = build_evaluator_prompt(
            conversation_text=conversation_text,
            current_facts=format_current_facts(facts),
            family_facts=family_facts_text,
        )
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

        # Idempotence guard. The prompt embeds both the conversation delta and
        # the agent's current facts, so an identical hash means identical input
        # and would produce identical ops -- there is nothing new to extract and
        # the CLI call is pure cost. Catches any double-trigger path, not just
        # the checkpoint/rotation collision fixed in account_turn_and_maybe_eval.
        # Only `success` rows count, so a failed run is still retried.
        if await self._already_extracted(user_id, prompt_hash):
            logger.info(
                "Evaluator skipped: identical prompt already extracted",
                agent_id=agent_id,
                prompt_hash=prompt_hash,
                session_id=session_id,
            )
            return True

        try:
            raw_response = await self._invoke_cli(prompt, backend=backend)
        except Exception as e:
            await self._handle_failure(
                session_id, user_id, prompt_hash, cumulative_tokens, str(e), agent_id
            )
            return False

        try:
            ops = validate_evaluator_output(
                raw_response, allow_family=family_scope_id is not None
            )
        except (ValueError, json.JSONDecodeError) as e:
            await self._handle_failure(
                session_id,
                user_id,
                prompt_hash,
                cumulative_tokens,
                f"Validation failed: {e}",
                agent_id,
                raw_response=raw_response,
            )
            return False

        # ---- Pipeline steps 1+3 (dv-c5d9): candidates -> reconcile -> ops.
        # `supersede` from extraction is demoted to a candidate carrying a
        # hint; `flag_conflict` from extraction is dropped (the reconciler
        # decides conflicts, review R5); `invalidate` passes through — a fact
        # that became false with nothing replacing it has no candidate for
        # the reconciler to judge.
        candidates, passthrough, rejected_episodic = self._split_ops(ops, agent_id)

        reconcile_status = "skipped"
        verdicts: Optional[List[Verdict]] = None
        gate: Optional[GateOutcome] = None
        if candidates and self._reconcile_enabled and self._jev_gate is not None:
            gate = await run_gate(
                get_router(),
                candidates,
                {"private": facts, "family": family_facts},
                self._jev_gate,
            )
        if candidates and self._reconcile_enabled:
            try:
                verdicts = await self._reconcile(
                    candidates, gate, format_current_facts(facts), family_facts_text
                )
                reconcile_status = "ok"
            except Exception as e:
                # Fallback: v1 behaviour (trust extraction hints). A
                # reconciler outage must never drop extracted facts.
                reconcile_status = f"failed: {str(e)[:200]}"
                logger.warning(
                    "Reconciler failed, applying candidates with hints",
                    agent_id=agent_id,
                    error=str(e),
                )

        active_ids_by_scope: Dict[str, Set[int]] = {
            "private": {f.id for f in facts},
            "family": {f.id for f in family_facts},
        }
        if verdicts is not None:
            mapped = map_verdicts(candidates, verdicts, active_ids_by_scope)
        else:
            mapped = self._map_with_hints(candidates, active_ids_by_scope)

        final_ops = passthrough + mapped.ops
        result = await self._writer.apply_ops(
            agent_id, final_ops, family_agent_id=family_scope_id
        )

        conflicts_flagged = await self._flag_safety_conflicts(
            mapped, result, offset=len(passthrough)
        )

        slice_saved = await self._slice_saved_ids(
            agent_id, family_scope_id, since_message_id
        )
        op_stats = self._build_op_stats(
            # "found" counts everything extraction emitted, so the report's
            # parts (incl. episodic rejections) reconcile with it.
            found=len(candidates) + len(rejected_episodic),
            mapped=mapped,
            result=result,
            conflicts_flagged=conflicts_flagged,
            slice_saved=slice_saved,
            reconcile_status=reconcile_status,
            verdicts=verdicts,
            rejected_episodic=rejected_episodic,
            gate=gate,
        )

        target_files = [agent_id]
        if family_scope_id and any(op.get("scope") == "family" for op in final_ops):
            target_files.append(family_scope_id)

        row_id = await self._record_extraction(
            session_id=session_id,
            user_id=user_id,
            prompt_hash=prompt_hash,
            raw_response=raw_response,
            parsed_insights=final_ops,
            target_files=target_files,
            cumulative_tokens=cumulative_tokens,
            status="success",
            op_stats=op_stats,
        )

        # D9: report every run that FOUND something, including "5 found,
        # 5 already saved, 0 new" — silence is how the honesty gap formed.
        # A run whose only output was episodic rejections still reports
        # (found includes them), keeping guard misfires visible.
        if (op_stats["found"] or result.applied) and self._notify:
            try:
                await self._notify(user_id, _format_report(op_stats), agent_id)
            except Exception:
                pass  # Notification failure is not critical

        logger.info(
            "Evaluator completed",
            agent_id=agent_id,
            applied=result.applied,
            op_stats=op_stats,
        )
        if self._screen.enabled and row_id is not None:
            self._spawn_shadow(
                row_id=row_id,
                agent_id=agent_id,
                backend=backend,
                conversation_text=conversation_text,
                facts=facts,
                family_facts=family_facts,
                family_scope_id=family_scope_id,
                since_message_id=since_message_id,
            )
        return True

    def _split_ops(
        self, ops: List[Dict[str, Any]], agent_id: str
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, str]]]:
        """Extraction ops -> (candidates, passthrough, episodic rejections).

        ``supersede`` from extraction is demoted to a candidate carrying a
        hint; ``flag_conflict`` from extraction is dropped (the reconciler
        decides conflicts, review R5); ``invalidate`` passes through. Shared
        by the classic path and the Jev screen's shadow run.
        """
        candidates: List[Dict[str, Any]] = []
        passthrough: List[Dict[str, Any]] = []
        rejected_episodic: List[Dict[str, str]] = []
        for op in ops:
            if op["op"] in ("add", "supersede"):
                cand = dict(op)
                cand["op"] = "add"
                # Deterministic episodic backstop (dv-7517 step 3), behind
                # the prompt's durability rules. Rejections are logged with
                # FULL content and surfaced in op_stats/report so every
                # misfire is auditable from Telegram/journal.
                if self._episodic_guard_enabled:
                    reason = classify_episodic(
                        str(cand.get("content") or ""),
                        tier=str(cand.get("tier") or "general"),
                    )
                    if reason is not None:
                        rejected_episodic.append(
                            {"reason": reason, "content": str(cand["content"])}
                        )
                        logger.warning(
                            "Episodic candidate rejected",
                            agent_id=agent_id,
                            reason=reason,
                            content=str(cand["content"]),
                        )
                        continue
                candidates.append(cand)
            elif op["op"] == "invalidate":
                passthrough.append(dict(op))
            else:
                logger.warning(
                    "Extraction-emitted op dropped (reconciler decides)",
                    op=op.get("op"),
                    agent_id=agent_id,
                )
        return candidates, passthrough, rejected_episodic

    # ------------------------------------------------------------------
    # Jev screen, shadow mode (dv-7e11 step c)
    # ------------------------------------------------------------------

    def _spawn_shadow(self, **kwargs: Any) -> None:
        """Run the screen beside the classic path as a detached task."""
        task = asyncio.create_task(self._shadow_screen(**kwargs))
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    async def _shadow_screen(
        self,
        *,
        row_id: int,
        agent_id: str,
        backend: str,
        conversation_text: str,
        facts: List,
        family_facts: List,
        family_scope_id: Optional[str],
        since_message_id: Optional[int],
    ) -> None:
        """Screen + targeted extraction over the same slice the classic path
        just handled; proposes ops, writes NO facts, and merges its record
        into the classic row's ``op_stats.screen``. Every failure is caught:
        the classic path is already done and must stay untouched."""
        try:
            stats = await self._screen_run(
                agent_id=agent_id,
                backend=backend,
                conversation_text=conversation_text,
                facts=facts,
                family_facts=family_facts,
                family_scope_id=family_scope_id,
                since_message_id=since_message_id,
            )
        except Exception as e:  # noqa: BLE001 - shadow must never raise
            logger.warning("Screen shadow failed", agent_id=agent_id, error=str(e))
            stats = {
                "mode": self._screen.mode,
                "status": "error",
                "error": str(e)[:300],
            }
        try:
            await self._extractions.set_op_stats_key(row_id, "screen", stats)
        except Exception as e:  # noqa: BLE001
            logger.warning("Screen shadow not recorded", error=str(e))

    async def _screen_run(
        self,
        *,
        agent_id: str,
        backend: str,
        conversation_text: str,
        facts: List,
        family_facts: List,
        family_scope_id: Optional[str],
        since_message_id: Optional[int],
    ) -> Dict[str, Any]:
        cfg = self._screen
        chunks = chunk_turns(parse_turns(conversation_text), cfg.chunk_tokens)
        remember_ids = await self._slice_saved_ids(
            agent_id, family_scope_id, since_message_id
        )
        # The slice's own dates (first message .. now: the shadow runs right
        # after the slice). Per-turn times are not in conversation_text.
        date_note = chunk_date_note(
            await self._message_timestamp(since_message_id), datetime.now(UTC)
        )
        out: List[Dict[str, Any]] = []
        for index, chunk in enumerate(chunks[:MAX_CHUNKS_PER_RUN]):
            out.append(
                await self._screen_chunk(
                    index=index,
                    chunk=chunk,
                    agent_id=agent_id,
                    backend=backend,
                    facts=facts,
                    family_facts=family_facts,
                    family_scope_id=family_scope_id,
                    remember_ids=remember_ids,
                    date_note=date_note,
                )
            )
        return {
            "mode": cfg.mode,
            "status": "ok",
            "send_min": cfg.fact_send_min,
            "send_max": cfg.fact_send_max,
            "chunks": out,
            "chunks_skipped": max(0, len(chunks) - MAX_CHUNKS_PER_RUN),
        }

    async def _screen_chunk(
        self,
        *,
        index: int,
        chunk: List,
        agent_id: str,
        backend: str,
        facts: List,
        family_facts: List,
        family_scope_id: Optional[str],
        remember_ids: Set[int],
        date_note: str = "",
    ) -> Dict[str, Any]:
        """One chunk: Jev scores -> fact selection -> targeted Sonnet call ->
        proposed ops. Jev failure = give up on the chunk (logged, no ops)."""
        cfg = self._screen
        all_facts = list(facts) + list(family_facts)
        record: Dict[str, Any] = {"index": index, "turns": len(chunk)}
        state = build_state(agent_key(agent_id), chunk)
        gate = content_questions(agent_key(agent_id))
        calls = pack_calls(state, gate, fact_questions(all_facts))
        try:
            scores = await score_chunk(get_router(), state, calls)
        except Exception as e:  # noqa: BLE001 - Jev down: this chunk only
            logger.warning("Screen: Jev failed, chunk skipped", error=str(e))
            record.update(status="jev_failed", error=str(e)[:300])
            return record

        selection = select_facts(all_facts, scores, cfg, remember_ids)
        private_ids = {f.id for f in facts}
        selected_private = [f for f in selection.facts if f.id in private_ids]
        selected_family = [f for f in selection.facts if f.id not in private_ids]
        record.update(
            status="ok",
            jev={
                "calls": scores.calls,
                "questions": len(scores.answers),
                "input_tokens": scores.input_tokens,
                "cost_usd": round(scores.cost_usd, 8),
                "ms": scores.ms,
            },
            gate={q: round(scores.answers.get(q, 0.0), 3) for q in gate},
            facts={
                "total": len(all_facts),
                "over_threshold": selection.over_threshold,
                "capped": selection.capped,
                "sent": len(selection.facts),
                "sent_chars": sum(len(f.content) for f in selection.facts),
                "total_chars": sum(len(f.content) for f in all_facts),
            },
            selected=[
                {
                    "id": f.id,
                    "t": round(scores.fact_scores(f.id)[0], 2),
                    "m": round(scores.fact_scores(f.id)[1], 2),
                    "why": selection.reasons[f.id],
                }
                for f in selection.facts
            ],
            # Wider net than the send set, for recall analysis at other cuts.
            scores={
                str(f.id): [round(v, 2) for v in scores.fact_scores(f.id)]
                for f in all_facts
                if max(scores.fact_scores(f.id)) >= 0.1
            },
        )

        prompt = build_evaluator_prompt(
            conversation_text=chunk_text(chunk),
            current_facts=date_note
            + SELECTED_FACTS_HEADER
            + format_current_facts(selected_private),
            family_facts=(
                format_current_facts(selected_family)
                if family_scope_id is not None
                else None
            ),
        )
        record["prompt_chars"] = len(prompt)
        try:
            raw = await self._invoke_cli(
                prompt, backend=backend, purpose="screen_extraction"
            )
            ops = validate_evaluator_output(
                raw, allow_family=family_scope_id is not None
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Screen: targeted extraction failed", error=str(e))
            record.update(status="extract_failed", error=str(e)[:300])
            return record

        record.update(
            self._propose_ops(
                ops,
                agent_id,
                {f.id for f in selected_private},
                {f.id for f in selected_family},
                {f.id: str(f.content) for f in selection.facts},
            )
        )
        return record

    def _propose_ops(
        self,
        ops: List[Dict[str, Any]],
        agent_id: str,
        shown_private: Set[int],
        shown_family: Set[int],
        shown_text: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        """Sonnet's ops on the targeted list -> the ops the classic mapping
        would apply (verdicts built from Sonnet's own fields, then the
        unchanged ``map_verdicts``). Nothing is written."""
        shown = shown_private | shown_family
        shown_text = shown_text or {}
        candidates, passthrough, rejected = self._split_ops(ops, agent_id)

        # Task/ticket status belongs on the board, not in memory (owner rule;
        # the classic prompt states it, the guard counts it). Screen path only.
        kept: List[Dict[str, Any]] = []
        ticket_rejected: List[str] = []
        for cand in candidates:
            if classify_ticket_status(
                str(cand.get("content") or ""), str(cand.get("tier") or "general")
            ):
                ticket_rejected.append(str(cand.get("content")))
                logger.warning(
                    "Screen: ticket-status candidate rejected",
                    agent_id=agent_id,
                    content=str(cand.get("content")),
                )
            else:
                kept.append(cand)
        candidates = kept

        # Compound-fact rule (b2 w214/f991): closing a multi-claim fact may
        # lose only the changed claim; the rest must be re-added in this run.
        added_texts = [str(c.get("content") or "") for c in candidates]
        compound_downgraded: List[int] = []
        for cand in candidates:
            hint = cand.get("supersedes_id")
            if (
                isinstance(hint, int)
                and hint in shown_text
                and not closes_compound_safely(shown_text[hint], added_texts)
            ):
                compound_downgraded.append(hint)
                del cand["supersedes_id"]
        referenced: Set[int] = {i for i in compound_downgraded}
        verdicts: List[Verdict] = []
        for i, cand in enumerate(candidates):
            supersedes = cand.get("supersedes_id")
            conflicts = cand.get("conflicts_with_id")
            existing = supersedes if isinstance(supersedes, int) else conflicts
            referenced |= {x for x in (supersedes, conflicts) if isinstance(x, int)}
            if isinstance(existing, int):
                verdicts.append(
                    Verdict(
                        index=i,
                        verdict="contradicts",
                        existing_id=existing,
                        safety=isinstance(conflicts, int),
                        revokes=bool(cand.get("revokes")),
                    )
                )
            else:
                verdicts.append(Verdict(index=i, verdict="new"))
        mapped = map_verdicts(
            candidates,
            verdicts,
            {"private": shown_private, "family": shown_family},
        )
        kept_invalidations: List[Dict[str, Any]] = []
        dropped_invalidations: List[Dict[str, Any]] = []
        redundant_invalidations: List[int] = []
        compound_rejected: List[int] = []
        superseded_ids = {
            o["supersedes_id"] for o in mapped.ops if o.get("op") == "supersede"
        }
        for op in passthrough:
            referenced.add(int(op["target_id"]))
            if op["target_id"] in superseded_ids:
                # Same id also superseded in this run: the supersede closes it.
                redundant_invalidations.append(int(op["target_id"]))
                continue
            text = shown_text.get(op["target_id"])
            if text is not None and not closes_compound_safely(text, added_texts):
                compound_rejected.append(int(op["target_id"]))
                continue
            # Design guard: an invalidate may only target a fact Sonnet saw.
            scope_ids = shown_family if op.get("scope") == "family" else shown_private
            target = (
                kept_invalidations
                if op["target_id"] in scope_ids
                else dropped_invalidations
            )
            target.append(op)
        proposed = [_op_summary(op) for op in kept_invalidations + mapped.ops]
        return {
            "ops": proposed,
            "safety_pairs": [list(p) for p in mapped.safety_flags],
            "rejected_episodic": len(rejected),
            "rejected_ticket_status": ticket_rejected,
            "compound_downgraded": compound_downgraded,
            "compound_rejected_invalidations": compound_rejected,
            "redundant_invalidations": redundant_invalidations,
            "referenced_not_sent": sorted(referenced - shown),
            "dropped_invalidations": [
                int(o["target_id"]) for o in dropped_invalidations
            ],
        }

    async def _reconcile(
        self,
        candidates: List[Dict[str, Any]],
        gate: Optional[GateOutcome],
        current_facts: str,
        family_facts_text: Optional[str],
    ) -> List[Verdict]:
        """Verdicts for every candidate: the haiku reconciler over all of
        them (gate off), or Jev's accepted verdicts plus one haiku batch over
        the escalated ones only (dv-7e11). Raises when the haiku call fails,
        so the caller's hint fallback stays the one outage path."""
        if gate is None:
            return await self._reconciler.reconcile(
                candidates, current_facts, family_facts_text
            )
        verdicts: Dict[int, Verdict] = dict(gate.accepted)
        if gate.escalated:
            batch = [candidates[i] for i in gate.escalated]
            escalated = await self._reconciler.reconcile(
                batch, current_facts, family_facts_text
            )
            for original, verdict in zip(gate.escalated, escalated):
                verdict.index = original
                verdicts[original] = verdict
        return [verdicts[i] for i in range(len(candidates))]

    @staticmethod
    def _map_with_hints(
        candidates: List[Dict[str, Any]],
        active_ids_by_scope: Dict[str, Set[int]],
    ) -> MappedOps:
        """Reconciler-down fallback: trust extraction's supersedes_id hints.

        A hint pointing at a fact that is not active in the candidate's own
        scope degrades to a plain add — same guardrail as map_verdicts.
        """
        mapped = MappedOps()
        for cand in candidates:
            scope = str(cand.get("scope") or "private")
            hint = cand.get("supersedes_id")
            op = {k: v for k, v in cand.items() if k != "supersedes_id"}
            if isinstance(hint, int) and hint in active_ids_by_scope.get(scope, set()):
                op["op"] = "supersede"
                op["supersedes_id"] = hint
            else:
                op["op"] = "add"
            mapped.ops.append(op)
        return mapped

    async def _flag_safety_conflicts(
        self, mapped: MappedOps, result: ApplyResult, offset: int
    ) -> int:
        """Pair each safety-contradiction add with the row it contradicts.

        Both rows stay active (owner step-4 decision): ``conflict_group`` is
        set on both; the window bundle injects the pair so the agent
        clarifies conversationally. Runs after apply_ops because the new
        row's id only exists then.
        """
        flagged = 0
        for existing_id, op_index in mapped.safety_flags:
            outcome = result.outcomes[offset + op_index]
            if outcome.outcome != "added" or not outcome.fact_id:
                continue  # dropped as lexical duplicate — nothing to flag
            try:
                await self._repo.flag_conflict(
                    existing_id, outcome.fact_id, outcome.scope
                )
                flagged += 1
                logger.info(
                    "Safety conflict flagged",
                    agent_id=outcome.scope,
                    existing_id=existing_id,
                    new_id=outcome.fact_id,
                )
            except Exception as e:
                logger.error("flag_conflict failed", error=str(e))
        return flagged

    async def _slice_saved_ids(
        self,
        agent_id: str,
        family_scope_id: Optional[str],
        since_message_id: Optional[int],
    ) -> Set[int]:
        """Ids of facts the in-session `remember` tool saved during the slice
        (dv-c5d9 step 1: memory_facts rows with source='agent_tool' created
        after the checkpoint boundary)."""
        since = await self._message_timestamp(since_message_id)
        try:
            ids = {f.id for f in await self._repo.get_agent_tool_facts(agent_id, since)}
            if family_scope_id:
                ids |= {
                    f.id
                    for f in await self._repo.get_agent_tool_facts(
                        family_scope_id, since
                    )
                }
            return ids
        except Exception as e:
            logger.warning("Slice-saved fact collection failed", error=str(e))
            return set()

    async def _message_timestamp(self, message_id: Optional[int]) -> Optional[datetime]:
        if not message_id:
            return None
        try:
            value = await self._messages.get_message_timestamp(message_id)
            if value is not None:
                if isinstance(value, datetime):
                    return value
                return datetime.fromisoformat(str(value))
        except Exception as e:
            logger.warning("Checkpoint timestamp lookup failed", error=str(e))
        return None

    @staticmethod
    def _build_op_stats(
        found: int,
        mapped: MappedOps,
        result: ApplyResult,
        conflicts_flagged: int,
        slice_saved: Set[int],
        reconcile_status: str,
        verdicts: Optional[List[Verdict]],
        rejected_episodic: Optional[List[Dict[str, str]]] = None,
        gate: Optional[GateOutcome] = None,
    ) -> Dict[str, Any]:
        """The per-run breakdown persisted to memory_extractions.op_stats.

        `already_saved` = reconciler-confirmed duplicates; `dropped_duplicate`
        = the writer's lexical backstop. `slice_saved_matches` counts the
        subset of already_saved that matched a fact the `remember` tool wrote
        during this very slice ("did my remember() work").
        `rejected_episodic` entries carry the FULL rejected content + reason
        (dv-7517: guard misfires must be auditable from the audit row).
        """
        matched_ids = list(mapped.already_saved.values())
        verdict_rows: List[Dict[str, Any]] = []
        for v in verdicts or []:
            row: Dict[str, Any] = {
                "index": v.index,
                "verdict": v.verdict,
                "existing_id": v.existing_id,
                "safety": v.safety,
            }
            if gate is not None:
                row["by"] = "jev" if v.index in gate.accepted else "haiku"
                row["gate_reason"] = gate.reasons.get(v.index)
                row["jev_confidence"] = gate.confidence.get(v.index)
            verdict_rows.append(row)
        stats: Dict[str, Any] = {
            "found": found,
            "rejected_episodic": len(rejected_episodic or []),
            "rejected_episodic_detail": rejected_episodic or [],
            "already_saved": len(mapped.already_saved),
            "added": result.added,
            "superseded": result.superseded,
            "invalidated": result.invalidated,
            "conflicts_flagged": conflicts_flagged,
            "dropped_duplicate": result.dropped_duplicate,
            "slice_saved_matches": sum(1 for i in matched_ids if i in slice_saved),
            "reconcile": reconcile_status,
            "verdicts": verdict_rows,
        }
        if gate is not None:
            # dv-7e11: accepted vs escalated and why (phase 5 dashboard).
            stats["reconcile_gate"] = gate.stats()
        return stats

    def _bound_delta(self, conversation_text: str, agent_id: str) -> str:
        """Cap the conversation slice, keeping the most recent messages.

        `last_checkpoint_message_id` only advances after a successful run, so
        every failure makes the next prompt larger — without a ceiling one
        wedged extractor grows the delta until the CLI call itself fails. The
        cut keeps the tail (recent conversation is where new facts are) and
        lands on a turn boundary where one is available.

        Truncation is a data-loss event — facts in the dropped head are never
        re-sent — so it logs at warning level rather than silently trimming.
        """
        if len(conversation_text) <= self._max_delta_chars:
            return conversation_text

        dropped = len(conversation_text) - self._max_delta_chars
        tail = conversation_text[-self._max_delta_chars :]
        boundary = tail.find("\n\nUser: ")
        if boundary != -1:
            dropped += boundary
            tail = tail[boundary + 2 :]
        logger.warning(
            "Evaluator conversation delta truncated",
            agent_id=agent_id,
            original_chars=len(conversation_text),
            kept_chars=len(tail),
            dropped_chars=dropped,
            max_delta_chars=self._max_delta_chars,
        )
        return f"[earlier conversation omitted: {dropped} characters]\n\n{tail}"

    async def _invoke_cli(
        self, prompt: str, backend: str = "claude", purpose: str = "extraction"
    ) -> str:
        """One extraction call through the model router; returns the op list
        as a bare JSON array.

        ``backend`` is the chat's agent backend, a hint for the route
        (extraction follows the chat by default). The route's harness, model
        and structured-output form are the router's business: it wraps and
        strictifies :data:`EXTRACTION_SCHEMA` where a harness needs it, and
        unwraps the answer (dv-781f). It also strips the harness (no
        CLAUDE.md, skills, MCP servers or tools; measured 2026-08-04, that
        floor was ~42k tokens on runs that returned ``[]``), stops the child
        on timeout and records the call. ``purpose`` is only ever
        non-default for the screen's shadow call, so its cost is separable
        in ``model_calls``.
        """
        result = await get_router().complete(
            CallRequest(
                purpose=purpose,
                backend=backend,
                prompt=prompt,
                system_prompt=EXTRACTION_SYSTEM_PROMPT,
                json_schema=EXTRACTION_SCHEMA,
                timeout_seconds=self._timeout,
            )
        )
        return result.text

    async def _invoke_reconcile_cli(self, prompt: str) -> str:
        """The reconciler's transport: the reconcile route (Claude haiku by
        default, owner D4), whatever backend extraction used: a verdict list
        needs no tools and no project context."""
        result = await get_router().complete(
            CallRequest(
                purpose="reconcile",
                prompt=prompt,
                system_prompt=RECONCILE_SYSTEM_PROMPT,
                timeout_seconds=self._timeout,
            )
        )
        return result.text

    async def _handle_failure(
        self,
        session_id: str,
        user_id: int,
        prompt_hash: str,
        cumulative_tokens: int,
        error_msg: str,
        agent_id: str = "",
        raw_response: Optional[str] = None,
    ) -> None:
        """Record failure in the audit table and notify the user."""
        logger.error("Evaluator failed", error=error_msg, session_id=session_id)

        await self._record_extraction(
            session_id=session_id,
            user_id=user_id,
            prompt_hash=prompt_hash,
            cumulative_tokens=cumulative_tokens,
            status="failed",
            error_message=error_msg,
            raw_response=raw_response,
        )

        if self._notify:
            try:
                await self._notify(
                    user_id, f"Evaluator failed: {error_msg[:200]}", agent_id or None
                )
            except Exception:
                pass

    async def _already_extracted(self, user_id: int, prompt_hash: str) -> bool:
        """True when this exact prompt has already been extracted successfully.

        Failures are deliberately not counted: a run that errored or produced
        unparseable JSON must still be retried on the next trigger.
        """
        try:
            return await self._extractions.already_extracted(user_id, prompt_hash)
        except Exception as e:
            # Never let the guard block extraction; worst case we re-run.
            logger.warning("Extraction dedupe check failed", error=str(e))
            return False

    async def _record_extraction(
        self,
        session_id: str,
        user_id: int,
        prompt_hash: str,
        cumulative_tokens: int,
        status: str,
        raw_response: Optional[str] = None,
        parsed_insights: Optional[List] = None,
        target_files: Optional[List] = None,
        error_message: Optional[str] = None,
        op_stats: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Record extraction attempt in memory_extractions table; returns the
        row id (None when the write failed)."""
        try:
            return await self._extractions.record_extraction(
                session_id=session_id,
                user_id=user_id,
                prompt_hash=prompt_hash,
                raw_response=raw_response,
                parsed_insights_json=(
                    json.dumps(parsed_insights) if parsed_insights else None
                ),
                validation_status=status,
                target_files_json=json.dumps(target_files) if target_files else None,
                error_message=error_message,
                cumulative_tokens_at_run=cumulative_tokens,
                op_stats_json=json.dumps(op_stats) if op_stats else None,
            )
        except Exception as e:
            logger.error("Failed to record extraction", error=str(e))
            return None


def _op_summary(op: Dict[str, Any]) -> Dict[str, Any]:
    """A proposed op, trimmed to what the shadow report compares."""
    keys = ("op", "scope", "tier", "kind", "content", "supersedes_id", "target_id")
    return {k: op[k] for k in keys if op.get(k) is not None}


def _format_report(stats: Dict[str, Any]) -> str:
    """The user-facing per-run breakdown (dv-c5d9 step 6, owner D9).

    Must render exactly the numbers persisted in op_stats — an inaccurate
    memory report is the dv-7783 failure class.
    """
    parts = [
        f"{stats['already_saved']} already saved",
        f"{stats['added']} new",
        f"{stats['superseded']} superseded",
        f"{stats['dropped_duplicate']} dropped as duplicate",
    ]
    if stats.get("invalidated"):
        parts.append(f"{stats['invalidated']} invalidated")
    if stats.get("conflicts_flagged"):
        parts.append(f"{stats['conflicts_flagged']} safety conflict(s) flagged")
    if stats.get("rejected_episodic"):
        parts.append(f"{stats['rejected_episodic']} episodic rejected")
    return f"Memory: {stats['found']} found — " + ", ".join(parts)
