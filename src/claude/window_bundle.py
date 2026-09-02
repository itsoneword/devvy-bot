"""Frozen per-window context bundle provider (dv-5e2a).

One system prompt per conversation window, materialized on the window's first
turn (get-or-create by ``(user_id, working_dir, window_id)``) and reused
byte-identical on every later turn -- including across restarts, which reload
the persisted row instead of recomposing. Persona or fact edits therefore take
effect at the next roll (or explicit /new), never mid-window.

Growth is handled by a pressure valve, not a cap: when the facts snapshot
exceeds its character budget the bundle is composed and injected anyway, and a
consolidation run is requested (dv-4c6f) -- never truncation, never rejection.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import structlog

from src.storage.memory_repository import (
    FactsSnapshot,
    render_fact_line,
    render_pointer_line,
)
from src.storage.window_bundle_repository import WindowBundle

from .prompt_composer import PromptComposer

if TYPE_CHECKING:
    from src.consolidation.service import ConsolidationService
    from src.profiles.registry import ProfileRegistry
    from src.storage.memory_repository import MemoryRepository
    from src.storage.window_bundle_repository import WindowBundleRepository
    from src.storage.window_repository import WindowRepository

logger = structlog.get_logger()


@dataclass
class BundleResult:
    """A frozen bundle plus the metadata instrumentation reports on."""

    text: str
    sha256: str
    window_id: int
    agent_id: str
    persona_chars: int
    facts_count: int
    pointers_count: int
    family_count: int
    total_chars: int
    reused: bool


class WindowBundleProvider:
    """Window lookup -> bundle row fetch -> compose-and-persist on miss.

    Injected into both agent backends (Claude SDK + Codex CLI) so a codex
    turn in the same window cannot un-freeze the prompt. Resolution of
    composer/agent-id per working dir mirrors the backends' own logic.
    """

    def __init__(
        self,
        window_repo: "WindowRepository",
        bundle_repo: "WindowBundleRepository",
        memory_repo: "MemoryRepository",
        profile_registry: Optional["ProfileRegistry"] = None,
        default_composer: Optional[PromptComposer] = None,
        facts_budget_chars: int = 12_000,
        sandbox_default: bool = True,
    ) -> None:
        self._window_repo = window_repo
        self._bundle_repo = bundle_repo
        self._memory_repo = memory_repo
        self._registry = profile_registry
        self._default_composer = default_composer
        self._facts_budget_chars = facts_budget_chars
        self._sandbox_default = sandbox_default
        self._consolidation: Optional["ConsolidationService"] = None
        self._profile_composers: Dict[str, PromptComposer] = {}
        # Last materialized/reused bundle per (user_id, working_dir) -- feeds
        # the facade's memory_loaded lifecycle log without extra DB reads.
        self._last: Dict[Tuple[int, str], BundleResult] = {}
        # Pressure-valve dedupe per (agent_id, window_id); the service's own
        # per-scope/24h debounce is the real guard, this just avoids re-firing
        # on every restart-reload of an over-budget window.
        self._valve_fired: Set[Tuple[str, int]] = set()

    def attach_consolidation(self, service: "ConsolidationService") -> None:
        """Wire the pressure valve target (post-startup; scheduler-owned)."""
        self._consolidation = service

    def last_meta(
        self, user_id: int, working_directory: Path
    ) -> Optional[BundleResult]:
        """Metadata of the bundle last served for this key (in-memory)."""
        return self._last.get((user_id, str(Path(working_directory))))

    async def get_or_materialize(
        self,
        working_directory: Path,
        user_id: int,
        family_id: Optional[int] = None,
        full_access: bool = False,
    ) -> Optional[BundleResult]:
        """Return the frozen bundle for the caller's current window.

        None = no composer resolves for this working dir (single-profile
        deployments without soul/memory); callers keep their fallback prompt.
        """
        composer = self._composer_for(working_directory)
        if composer is None:
            return None

        state = await self._window_repo.get_or_create(user_id, working_directory)
        key = (user_id, str(Path(working_directory)))

        row = await self._bundle_repo.get(user_id, working_directory, state.window_id)
        if row is not None:
            result = BundleResult(
                text=row.bundle_text,
                sha256=row.bundle_sha256,
                window_id=row.window_id,
                agent_id=row.agent_id,
                persona_chars=row.persona_chars,
                facts_count=row.facts_count,
                pointers_count=row.pointers_count,
                family_count=row.family_count,
                total_chars=row.total_chars,
                reused=True,
            )
            self._last[key] = result
            logger.debug(
                "bundle_reused",
                window_id=row.window_id,
                agent_id=row.agent_id,
                sha256=row.bundle_sha256,
            )
            return result

        result = await self._materialize(
            composer=composer,
            working_directory=working_directory,
            user_id=user_id,
            window_id=state.window_id,
            family_id=family_id,
            full_access=full_access,
        )
        self._last[key] = result
        return result

    async def _materialize(
        self,
        composer: PromptComposer,
        working_directory: Path,
        user_id: int,
        window_id: int,
        family_id: Optional[int],
        full_access: bool,
    ) -> BundleResult:
        from src.profiles.registry import (
            agent_id_for_working_dir,
            family_agent_id_for_agent,
        )

        agent_id = agent_id_for_working_dir(self._registry, working_directory, user_id)
        snapshot = await self._memory_repo.facts_snapshot(agent_id)

        conflict_ids = {fact.id for pair in snapshot.conflicts for fact in pair}
        pinned_lines = [
            render_fact_line(f) for f in snapshot.pinned if f.id not in conflict_ids
        ]
        general_lines = [
            render_fact_line(f) for f in snapshot.general if f.id not in conflict_ids
        ]
        conflict_pairs = [
            (render_fact_line(a), render_fact_line(b)) for a, b in snapshot.conflicts
        ]
        pointer_lines = [render_pointer_line(p) for p in snapshot.pointers]

        family_lines: List[str] = []
        if family_id is not None:
            family_scope = family_agent_id_for_agent(agent_id, family_id)
            family_lines = self._family_lines(
                await self._memory_repo.facts_snapshot(family_scope)
            )

        text = composer.compose(
            working_directory=working_directory,
            pinned_facts=pinned_lines or None,
            general_facts=general_lines or None,
            family_facts=family_lines or None,
            result_pointers=pointer_lines or None,
            conflict_pairs=conflict_pairs or None,
            full_access=full_access,
            # Materialization happens on the window's first turn, so "now"
            # is the window start (dv-1106 amnesia-boundary stamp).
            window_started=datetime.now(UTC).strftime("%Y-%m-%d"),
        )

        facts_count = len(pinned_lines) + len(general_lines) + 2 * len(conflict_pairs)
        facts_chars = sum(
            len(line)
            for line in (
                pinned_lines
                + general_lines
                + family_lines
                + [line for pair in conflict_pairs for line in pair]
            )
        )
        self._check_budget(agent_id, window_id, facts_chars)

        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        persona_chars = composer.get_section_sizes().get("agent.md", 0)
        stored = await self._bundle_repo.put(
            WindowBundle(
                user_id=user_id,
                working_dir=str(Path(working_directory)),
                window_id=window_id,
                agent_id=agent_id,
                bundle_text=text,
                bundle_sha256=sha256,
                persona_chars=persona_chars,
                facts_count=facts_count,
                pointers_count=len(pointer_lines),
                family_count=len(family_lines),
                total_chars=len(text),
            )
        )
        logger.info(
            "bundle_chars",
            window_id=stored.window_id,
            agent_id=stored.agent_id,
            total_chars=stored.total_chars,
            persona_chars=stored.persona_chars,
            facts_count=stored.facts_count,
            pointers_count=stored.pointers_count,
            family_count=stored.family_count,
            sha256=stored.bundle_sha256,
        )
        return BundleResult(
            text=stored.bundle_text,
            sha256=stored.bundle_sha256,
            window_id=stored.window_id,
            agent_id=stored.agent_id,
            persona_chars=stored.persona_chars,
            facts_count=stored.facts_count,
            pointers_count=stored.pointers_count,
            family_count=stored.family_count,
            total_chars=stored.total_chars,
            reused=False,
        )

    @staticmethod
    def _family_lines(snapshot: FactsSnapshot) -> List[str]:
        seen: Set[int] = set()
        lines: List[str] = []
        for fact in snapshot.pinned + snapshot.general:
            if fact.id not in seen:
                seen.add(fact.id)
                lines.append(render_fact_line(fact))
        return lines

    def _check_budget(self, agent_id: str, window_id: int, facts_chars: int) -> None:
        """Pressure valve: over budget triggers consolidation, never drops."""
        if facts_chars <= self._facts_budget_chars:
            return
        if (agent_id, window_id) in self._valve_fired:
            return
        self._valve_fired.add((agent_id, window_id))
        if self._consolidation is not None:
            self._consolidation.request_run(
                agent_id,
                reason="window bundle facts snapshot over budget "
                f"({facts_chars} > {self._facts_budget_chars} chars)",
                snapshot_chars=facts_chars,
            )
        else:
            logger.warning(
                "Bundle facts over budget; no consolidation service attached",
                agent_id=agent_id,
                snapshot_chars=facts_chars,
                budget=self._facts_budget_chars,
            )

    def _composer_for(self, working_directory: Path) -> Optional[PromptComposer]:
        """Per-profile composer for a working dir, else the default.

        Mirrors the backends' resolution so redirecting them here changes
        where composition happens (once per window), not what is composed.
        """
        if not self._registry:
            return self._default_composer
        profile = self._registry.get_by_working_dir(working_directory)
        if not profile:
            return self._default_composer
        cached = self._profile_composers.get(profile.slug)
        if cached is None:
            cached = PromptComposer(
                memory_path=profile.memory_dir,
                agent_md_path=profile.persona,
            )
            self._profile_composers[profile.slug] = cached
        return cached
