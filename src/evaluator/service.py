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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

import structlog

from ..model_router import CLAUDE_HARNESS, CODEX_HARNESS, CallRequest, get_router
from ..profiles.registry import family_agent_id_for_agent
from ..storage.extraction_repository import MemoryExtractionRepository
from ..storage.repositories import MessageRepository
from .episodic_guard import classify as classify_episodic
from .prompts import build_evaluator_prompt, format_current_facts
from .reconciler import FactReconciler, MappedOps, Verdict, map_verdicts
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
        model: str = "sonnet",
        codex_model: str = "openai-codex/gpt-6-sol",
        max_delta_chars: int = 120_000,
        timeout_seconds: int = 120,
        reconcile_model: str = "haiku",
        reconcile_enabled: bool = True,
        episodic_guard_enabled: bool = True,
    ) -> None:
        self._repo = memory_repo
        self._writer = writer
        self._token_tracker = token_tracker
        self._db = db
        self._extractions = MemoryExtractionRepository(db)
        self._messages = MessageRepository(db)
        self._notify = notify_callback  # async fn(user_id, message, agent_id)
        self._model = model
        # Extraction on the Codex backend. Always passed to the CLI: without
        # it the call ran whatever ~/.codex/config.toml named.
        self._codex_model = codex_model
        self._max_delta_chars = max_delta_chars
        # A timeout stops the CLI child (it used to leak) and surfaces as a
        # 'timeout ...' error_message so the rate is queryable (plan D6).
        self._timeout = timeout_seconds
        # Reconciler is a second haiku call (owner D4); haiku needs no gate
        # here — "a comparison between two short texts, mechanical".
        self._reconcile_model = reconcile_model
        self._reconcile_enabled = reconcile_enabled
        # Deterministic episodic backstop (dv-7517 step 3). Kill switch:
        # EVALUATOR_EPISODIC_GUARD=false — kept for the first weeks in case
        # the keyword heuristic misfires on live RU/EN phrasing.
        self._episodic_guard_enabled = episodic_guard_enabled
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

        reconcile_status = "skipped"
        verdicts: Optional[List[Verdict]] = None
        if candidates and self._reconcile_enabled:
            try:
                verdicts = await self._reconciler.reconcile(
                    candidates,
                    format_current_facts(facts),
                    family_facts_text,
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
        )

        target_files = [agent_id]
        if family_scope_id and any(op.get("scope") == "family" for op in final_ops):
            target_files.append(family_scope_id)

        await self._record_extraction(
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
        return True

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
        return {
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
            "verdicts": [
                {
                    "index": v.index,
                    "verdict": v.verdict,
                    "existing_id": v.existing_id,
                    "safety": v.safety,
                }
                for v in verdicts or []
            ],
        }

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

    async def _invoke_cli(self, prompt: str, backend: str = "claude") -> str:
        """Dispatch the extraction to whichever backend is active."""
        if backend == "openai_codex":
            return await self._invoke_codex_cli(prompt)
        return await self._invoke_claude_cli(prompt)

    async def _invoke_reconcile_cli(self, prompt: str) -> str:
        """The reconciler's transport: always Claude on the reconcile model
        (haiku, owner D4): a verdict list needs no tools and no project
        context, whatever backend extraction used."""
        return await self._invoke_claude_cli(
            prompt,
            model=self._reconcile_model,
            system_prompt=RECONCILE_SYSTEM_PROMPT,
            purpose="reconcile",
        )

    async def _invoke_claude_cli(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        purpose: str = "extraction",
    ) -> str:
        """One plain Claude call through the model router (dv-ef90).

        The router strips the harness (no CLAUDE.md, skills, MCP servers or
        tools; measured 2026-08-04, that floor was ~42k tokens on runs that
        returned ``[]``), sends the prompt over stdin, stops the child on
        timeout and records the call.
        """
        result = await get_router().complete(
            CallRequest(
                purpose=purpose,
                harness=CLAUDE_HARNESS,
                prompt=prompt,
                model=model or self._model,
                system_prompt=system_prompt or EXTRACTION_SYSTEM_PROMPT,
                timeout_seconds=self._timeout,
            )
        )
        return result.text

    def _codex_output_schema(self) -> dict:
        """JSON Schema matching validate_evaluator_output's op contract.

        OpenAI structured outputs reject a top-level ``type: "array"`` schema
        (the root must be an object), so the op list is wrapped under
        ``extractions`` and unwrapped again in :meth:`_unwrap_codex_output`.
        Every object node sets ``additionalProperties: False`` (OpenAI rule).

        Every property of the op object is also listed in ``required`` — that
        is the OTHER OpenAI strict-mode rule, and violating it is what made
        every Codex evaluator run fail (dv-0c01). The server answered 400
        ``invalid_json_schema``: "'required' is required to be supplied and to
        be an array including every key in properties. Missing 'label'."
        Optionality is expressed by the ``"null"`` member of each type union,
        never by omitting the key from ``required``; ``validate_evaluator_output``
        already treats an explicit null exactly like an absent field.
        """
        op_properties: dict = {
            "op": {
                "type": "string",
                "enum": ["add", "supersede", "invalidate", "flag_conflict"],
            },
            # tier/content are nullable: the metadata-only ops
            # (invalidate/flag_conflict) carry neither.
            "tier": {
                "type": ["string", "null"],
                "enum": ["pinned", "general", None],
            },
            "content": {"type": ["string", "null"]},
            "label": {"type": ["string", "null"]},
            "supersedes_id": {"type": ["integer", "null"]},
            "kind": {
                "type": ["string", "null"],
                "enum": ["user_fact", "conversation_result", None],
            },
            "source_ref": {"type": ["string", "null"]},
            "target_id": {"type": ["integer", "null"]},
            "ids": {
                "type": ["array", "null"],
                "items": {"type": "integer"},
            },
            "scope": {
                "type": ["string", "null"],
                "enum": ["private", "family", None],
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["extractions"],
            "properties": {
                "extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(op_properties),
                        "properties": op_properties,
                    },
                }
            },
        }

    @staticmethod
    def _unwrap_codex_output(text: str) -> str:
        """Unwrap the object-wrapped Codex response back to a bare JSON array.

        Tolerates a response that is already a bare array (defensive)."""
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return text
        if isinstance(parsed, dict) and "extractions" in parsed:
            return json.dumps(parsed["extractions"])
        return text

    async def _invoke_codex_cli(self, prompt: str) -> str:
        """One plain Codex call through the model router, constrained to the
        evaluator JSON schema. The router runs it read-only in a throwaway
        directory on ``codex_model``."""
        result = await get_router().complete(
            CallRequest(
                purpose="extraction",
                harness=CODEX_HARNESS,
                prompt=prompt,
                model=self._codex_model,
                system_prompt="",
                json_schema=self._codex_output_schema(),
                timeout_seconds=self._timeout,
            )
        )
        return self._unwrap_codex_output(result.text) if result.text else "[]"

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
    ) -> None:
        """Record extraction attempt in memory_extractions table."""
        try:
            await self._extractions.record_extraction(
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
