"""Family memory tier (users & families v1, Stage C).

Covers docs/plans/2026-07-19-users-families-v1.md "Family memory tier":
- family agent_id encoding (f{family_id}:{slug}, no schema change);
- write path: evaluator family scope selection — family user + explicit
  family framing may cross over; no family -> never; default stays private;
- read path: composer injects a separate labeled <family_facts> block only
  for family users, after private facts, under a split retrieval budget so
  family facts never starve private facts;
- /memory labels the private and family scopes distinctly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.claude.prompt_composer import PromptComposer
from src.evaluator.prompts import build_evaluator_prompt
from src.evaluator.service import EvaluatorService
from src.evaluator.validator import validate_evaluator_output
from src.evaluator.writer import MemoryWriter
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    family_agent_id,
    family_agent_id_for_agent,
    guest_agent_id,
)
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository, MemoryRetriever
from src.storage.user_repository import BotUserRepository, UserStore

OWNER_ID = 111
GUEST_ID = 222
FAMILY_ID = 3


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


class TestFamilyAgentIdEncoding:
    def test_family_agent_id_format(self):
        assert family_agent_id(FAMILY_ID, "chef") == f"f{FAMILY_ID}:chef"

    def test_owner_scope_maps_to_family_scope(self):
        assert family_agent_id_for_agent("chef", FAMILY_ID) == f"f{FAMILY_ID}:chef"

    def test_guest_scope_maps_to_same_family_scope(self):
        """Owner (slug) and guest (slug#uID) on one profile share ONE family
        scope — the write and read paths must meet on the same key."""
        guest_scope = guest_agent_id("chef", GUEST_ID)
        assert family_agent_id_for_agent(guest_scope, FAMILY_ID) == (
            family_agent_id_for_agent("chef", FAMILY_ID)
        )

    def test_family_scopes_are_disjoint_across_families(self):
        assert family_agent_id_for_agent("chef", 3) != family_agent_id_for_agent(
            "chef", 4
        )

    def test_family_scopes_are_disjoint_across_profiles(self):
        assert family_agent_id_for_agent("chef", 3) != family_agent_id_for_agent(
            "coach", 3
        )


# ---------------------------------------------------------------------------
# Validator: scope field, conservative defaults
# ---------------------------------------------------------------------------


class TestValidatorScope:
    def test_scope_defaults_private(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x"}]', allow_family=True
        )
        assert ops[0]["scope"] == "private"

    def test_family_scope_honored_when_allowed(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x","scope":"family"}]',
            allow_family=True,
        )
        assert ops[0]["scope"] == "family"

    def test_family_scope_coerced_private_without_family(self):
        """A user with no family can NEVER write family facts, even if the
        model hallucinates the scope."""
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x","scope":"family"}]',
            allow_family=False,
        )
        assert ops[0]["scope"] == "private"

    def test_null_scope_treated_as_private(self):
        """Codex structured output emits explicit nulls for omitted fields."""
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x","scope":null}]',
            allow_family=True,
        )
        assert ops[0]["scope"] == "private"

    def test_invalid_scope_entry_is_skipped_not_raised(self):
        """A single malformed entry is skipped, not fatal to the batch --
        see test_evaluator/test_validator.py for the batch-resilience case."""
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x","scope":"public"}]',
            allow_family=True,
        )
        assert ops == []


# ---------------------------------------------------------------------------
# Evaluator prompt: family instructions only for family users
# ---------------------------------------------------------------------------


class TestEvaluatorPromptFamily:
    def test_no_family_prompt_never_mentions_family(self):
        prompt = build_evaluator_prompt("User: hi", "(no existing facts)")
        assert "family" not in prompt.lower()

    def test_family_prompt_includes_scope_rules_and_shared_facts(self):
        prompt = build_evaluator_prompt(
            "User: hi", "(no existing facts)", family_facts="9 | general | we eat at 7"
        )
        assert "Current Family Facts" in prompt
        assert "we eat at 7" in prompt
        assert '"private" (default)' in prompt
        # Conservative default is spelled out for the model.
        assert 'ALWAYS choose\n  "private"' in prompt


# ---------------------------------------------------------------------------
# Write path: service + writer route family ops to the family scope
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    mgr = DatabaseManager(str(tmp_path / "mem.db"))
    await mgr.initialize()
    async with mgr.get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (123,))
        await conn.commit()
    yield mgr
    await mgr.close()


@pytest.fixture
def repo(db):
    return MemoryRepository(db)


@pytest.fixture
def service(repo, db):
    svc = EvaluatorService(
        memory_repo=repo,
        writer=MemoryWriter(repo),
        token_tracker=MagicMock(),
        db=db,
        notify_callback=None,
    )
    # Never hit the real CLI from a unit test. "[]" -> every candidate
    # defaults to verdict "new" (fact-preserving) -> plain adds; tests that
    # exercise reconcile verdicts override per-test.
    svc._invoke_reconcile_cli = AsyncMock(return_value="[]")
    return svc


FAMILY_OP = (
    '[{"op":"add","tier":"general",'
    '"content":"Anna: family dinner is at 19:00","scope":"family"}]'
)
PRIVATE_OP = '[{"op":"add","tier":"general","content":"likes spicy food"}]'


class TestEvaluatorFamilyWritePath:
    async def test_family_user_family_framed_fact_lands_in_family_scope(
        self, service, repo
    ):
        agent = guest_agent_id("chef", GUEST_ID)
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = FAMILY_OP
            ok = await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: remember for the family: dinner at 19",
                cumulative_tokens=55000,
                agent_id=agent,
                family_id=FAMILY_ID,
            )
        assert ok is True
        fam = await repo.get_facts(f"f{FAMILY_ID}:chef")
        assert [f.content for f in fam] == ["Anna: family dinner is at 19:00"]
        # Nothing leaked into the member's private scope.
        assert await repo.get_facts(agent) == []

    async def test_default_scope_stays_private_for_family_user(self, service, repo):
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = PRIVATE_OP
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: I love spicy food",
                cumulative_tokens=55000,
                agent_id="chef",
                family_id=FAMILY_ID,
            )
        assert [f.content for f in await repo.get_facts("chef")] == ["likes spicy food"]
        assert await repo.get_facts(f"f{FAMILY_ID}:chef") == []

    async def test_no_family_user_never_writes_family_scope(self, service, repo):
        """Even a family-scoped op from the model lands private when the
        user has no family."""
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = FAMILY_OP
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: ...",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        assert await repo.get_facts(f"f{FAMILY_ID}:chef") == []
        assert len(await repo.get_facts("chef")) == 1

    async def test_evaluator_sees_existing_family_facts(self, service, repo):
        await repo.add_fact(f"f{FAMILY_ID}:chef", "general", "we keep kosher")
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = "[]"
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: hi",
                cumulative_tokens=55000,
                agent_id="chef",
                family_id=FAMILY_ID,
            )
        prompt = cli.call_args[0][0]
        assert "we keep kosher" in prompt
        assert "Current Family Facts" in prompt

    async def test_family_supersede_targets_family_scope(self, service, repo):
        old_id = await repo.add_fact(f"f{FAMILY_ID}:chef", "general", "dinner at 18")
        op = (
            f'[{{"op":"supersede","tier":"general","content":"dinner at 19",'
            f'"supersedes_id":{old_id},"scope":"family"}}]'
        )
        # Post-R5 the reconciler decides the contradiction; the extraction
        # supersede above is only a hint.
        service._invoke_reconcile_cli = AsyncMock(
            return_value=(
                '[{"index":0,"verdict":"contradicts","existing_id":%d}]' % old_id
            )
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = op
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: we moved family dinner to 19",
                cumulative_tokens=55000,
                agent_id="chef",
                family_id=FAMILY_ID,
            )
        active = await repo.get_facts(f"f{FAMILY_ID}:chef")
        assert [f.content for f in active] == ["dinner at 19"]

    async def test_writer_without_family_scope_defaults_private(self, repo):
        writer = MemoryWriter(repo)
        applied = await writer.apply_ops(
            "chef",
            [
                {
                    "op": "add",
                    "tier": "general",
                    "content": "x",
                    "label": None,
                    "supersedes_id": None,
                    "scope": "family",
                }
            ],
            family_agent_id=None,
        )
        assert applied.applied == 1
        assert len(await repo.get_facts("chef")) == 1


# ---------------------------------------------------------------------------
# Read path: retrieval budget split + composer block
# ---------------------------------------------------------------------------


class TestFamilyRetrievalBudget:
    async def test_select_family_caps_at_family_budget(self, repo):
        scope = f"f{FAMILY_ID}:chef"
        for i in range(8):
            await repo.add_fact(scope, "general", f"family fact {i}")
        retriever = MemoryRetriever(repo)
        facts = await retriever.select_family(scope, query="fact")
        assert len(facts) == 5

    async def test_select_family_pinned_first(self, repo):
        scope = f"f{FAMILY_ID}:chef"
        await repo.add_fact(scope, "pinned", "kid allergic to nuts")
        for i in range(6):
            await repo.add_fact(scope, "general", f"family fact {i}")
        retriever = MemoryRetriever(repo)
        facts = await retriever.select_family(scope, query="")
        # Lines are date-prefixed [DD.MM.YY] (dv-8254 / D2).
        assert facts[0].split("] ", 1)[1] == "kid allergic to nuts"
        assert len(facts) == 5

    async def test_family_budget_does_not_shrink_private_selection(self, repo):
        """Private facts keep their full cap regardless of family volume."""
        for i in range(25):
            await repo.add_fact("chef", "general", f"private fact {i}")
        for i in range(8):
            await repo.add_fact(f"f{FAMILY_ID}:chef", "general", f"family fact {i}")
        retriever = MemoryRetriever(repo)
        _, general, _ = await retriever.select("chef", query="")
        family = await retriever.select_family(f"f{FAMILY_ID}:chef", query="")
        assert len(general) == 20
        assert len(family) == 5


class TestComposerFamilyBlock:
    @pytest.fixture(autouse=True)
    def _no_shared_capabilities(self, tmp_path, monkeypatch):
        """Isolate from the repo's data/shared-capabilities.md (which itself
        documents the family memory feature)."""
        monkeypatch.setattr(
            "src.claude.prompt_composer.SHARED_CAPABILITIES_PATH",
            tmp_path / "absent-shared.md",
        )

    def _composer(self, tmp_path):
        return PromptComposer(
            memory_path=tmp_path / "memory",
            agent_md_path=tmp_path / "agent.md",
        )

    def test_family_block_injected_with_label(self, tmp_path):
        prompt = self._composer(tmp_path).compose(
            working_directory=tmp_path,
            pinned_facts=["no peanuts"],
            general_facts=["likes stew"],
            family_facts=["Anna: dinner at 19:00"],
        )
        assert "<family_facts>" in prompt
        assert "Shared by family members" in prompt
        assert "Anna: dinner at 19:00" in prompt

    def test_family_block_after_private_facts(self, tmp_path):
        prompt = self._composer(tmp_path).compose(
            working_directory=tmp_path,
            pinned_facts=["no peanuts"],
            general_facts=["likes stew"],
            family_facts=["dinner at 19:00"],
        )
        assert prompt.index("<constraints>") < prompt.index("<user_facts>")
        assert prompt.index("<user_facts>") < prompt.index("<family_facts>")

    def test_no_family_facts_no_block(self, tmp_path):
        prompt = self._composer(tmp_path).compose(
            working_directory=tmp_path,
            pinned_facts=["no peanuts"],
            general_facts=["likes stew"],
        )
        assert "<family_facts>" not in prompt
        assert "Shared by family members" not in prompt


class TestSdkFamilyScopeResolution:
    def test_family_agent_id_for_working_dir(self, tmp_path):
        from src.claude.sdk_integration import ClaudeSDKManager
        from src.config import create_test_config
        from src.profiles.registry import ProfileDefinition, ProfileRegistry

        root = tmp_path / "chef"
        registry = ProfileRegistry(
            [
                ProfileDefinition(
                    slug="chef",
                    name="Chef",
                    working_dir=root,
                    memory_dir=root / "m",
                    persona=root / "p.md",
                    default=True,
                )
            ]
        )
        mgr = ClaudeSDKManager(create_test_config(), profile_registry=registry)
        # Owner root and a guest workspace resolve to the SAME family scope.
        assert mgr._family_agent_id_for(root, FAMILY_ID) == f"f{FAMILY_ID}:chef"
        guest_dir = root / "guests" / str(GUEST_ID)
        assert mgr._family_agent_id_for(guest_dir, FAMILY_ID) == f"f{FAMILY_ID}:chef"


# ---------------------------------------------------------------------------
# Orchestrator: family_id resolution + /memory labeling
# ---------------------------------------------------------------------------


def _build_orchestrator() -> MessageOrchestrator:
    settings = MagicMock()
    settings.agentic_mode = True
    return MessageOrchestrator(settings=settings, deps={})


def _user_store_stub(family_ids: dict) -> MagicMock:
    store = MagicMock()
    store.get_family_id_cached = MagicMock(side_effect=lambda uid: family_ids.get(uid))
    return store


class TestFamilyIdResolution:
    def test_family_user_resolves_id(self):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {"user_store": _user_store_stub({GUEST_ID: FAMILY_ID})}
        assert orch._family_id_for(context, GUEST_ID) == FAMILY_ID

    def test_no_family_resolves_none(self):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {"user_store": _user_store_stub({})}
        assert orch._family_id_for(context, GUEST_ID) is None

    def test_no_user_store_resolves_none(self):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {}
        assert orch._family_id_for(context, GUEST_ID) is None

    async def test_end_to_end_user_store_cache(self, tmp_path):
        """UserStore snapshot exposes family_id for the per-turn lookup."""
        db = DatabaseManager(str(tmp_path / "users.db"))
        await db.initialize()
        try:
            store = UserStore(BotUserRepository(db))
            family = await store.create_family(OWNER_ID)
            await store.add_user(
                GUEST_ID, role="guest", source="family", family_id=family.id
            )
            assert store.get_family_id_cached(GUEST_ID) == family.id
            assert store.get_family_id_cached(OWNER_ID) is None
        finally:
            await db.close()


class TestFamilyMemoryProfileOptOut:
    """A profile with family_memory: false must never see family scope,
    regardless of the speaking user's family membership -- e.g. a personal
    health coach where facts must stay strictly per-user even when the
    user explicitly frames something as a family fact."""

    def _registry(self, working_dir, *, family_memory: bool) -> ProfileRegistry:
        profile = ProfileDefinition(
            slug="coach",
            name="Coach",
            working_dir=working_dir,
            memory_dir=working_dir / "memory",
            persona=working_dir / "agent.md",
            default=True,
            family_memory=family_memory,
        )
        return ProfileRegistry([profile])

    def test_family_id_suppressed_for_opted_out_profile(self, tmp_path):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {
            "user_store": _user_store_stub({GUEST_ID: FAMILY_ID}),
            "profile_registry": self._registry(tmp_path, family_memory=False),
        }
        assert orch._family_id_for(context, GUEST_ID, tmp_path) is None

    def test_family_id_still_resolves_for_opted_in_profile(self, tmp_path):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {
            "user_store": _user_store_stub({GUEST_ID: FAMILY_ID}),
            "profile_registry": self._registry(tmp_path, family_memory=True),
        }
        assert orch._family_id_for(context, GUEST_ID, tmp_path) == FAMILY_ID

    def test_no_working_dir_falls_back_to_unfiltered_lookup(self, tmp_path):
        """Callers that can't resolve a working_dir (e.g. no profile
        registry configured) keep today's behavior -- only an explicit
        working_dir triggers the opt-out check."""
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {
            "user_store": _user_store_stub({GUEST_ID: FAMILY_ID}),
            "profile_registry": self._registry(tmp_path, family_memory=False),
        }
        assert orch._family_id_for(context, GUEST_ID) == FAMILY_ID

    def test_unknown_working_dir_falls_back_to_unfiltered_lookup(self, tmp_path):
        """A working_dir that doesn't map to any profile (e.g. single-profile
        mode drift) must not silently suppress family scope."""
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {
            "user_store": _user_store_stub({GUEST_ID: FAMILY_ID}),
            "profile_registry": self._registry(tmp_path, family_memory=False),
        }
        other_dir = tmp_path / "not-a-profile-root"
        assert orch._family_id_for(context, GUEST_ID, other_dir) == FAMILY_ID


class TestAgentIdForPersonalMemory:
    """Orchestrator._agent_id_for threads user_id through to
    agent_id_for_working_dir -- the actual fix for two "owner"-role family
    members (e.g. both spouses) otherwise sharing one private fact store
    on a profile like coach."""

    def _registry(self, working_dir, *, personal_memory: bool) -> ProfileRegistry:
        profile = ProfileDefinition(
            slug="coach",
            name="Coach",
            working_dir=working_dir,
            memory_dir=working_dir / "memory",
            persona=working_dir / "agent.md",
            default=True,
            personal_memory=personal_memory,
        )
        return ProfileRegistry([profile])

    def test_two_owners_get_distinct_agent_ids(self, tmp_path):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {
            "profile_registry": self._registry(tmp_path, personal_memory=True)
        }
        husband = orch._agent_id_for(context, tmp_path, OWNER_ID)
        wife = orch._agent_id_for(context, tmp_path, GUEST_ID)
        assert husband != wife
        assert husband == f"coach#u{OWNER_ID}"
        assert wife == f"coach#u{GUEST_ID}"

    def test_opted_out_profile_shares_one_agent_id(self, tmp_path):
        orch = _build_orchestrator()
        context = MagicMock()
        context.bot_data = {
            "profile_registry": self._registry(tmp_path, personal_memory=False)
        }
        husband = orch._agent_id_for(context, tmp_path, OWNER_ID)
        wife = orch._agent_id_for(context, tmp_path, GUEST_ID)
        assert husband == wife == "coach"


class TestMemoryCommandLabels:
    def _context(self, repo_facts, family_ids):
        composer = MagicMock()
        composer.get_section_sizes.return_value = {"agent.md": 400}
        storage = MagicMock()
        mem_repo = MagicMock()

        async def get_facts(agent_id, tier=None, status="active"):
            return [
                f
                for f in repo_facts.get(agent_id, [])
                if tier is None or f.tier == tier
            ]

        mem_repo.get_facts = AsyncMock(side_effect=get_facts)
        storage.memory_repository = mem_repo
        context = MagicMock()
        context.bot_data = {
            "prompt_composer": composer,
            "storage": storage,
            "user_store": _user_store_stub(family_ids),
        }
        context.user_data = {}
        return context

    def _update(self, user_id):
        update = MagicMock()
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        return update

    @staticmethod
    def _fact(content, tier="pinned", label=None):
        f = MagicMock()
        f.content = content
        f.tier = tier
        f.label = label
        return f

    async def test_family_user_sees_both_scopes_labeled(self):
        repo_facts = {
            "devops": [self._fact("private rule")],
            f"f{FAMILY_ID}:devops": [self._fact("family rule")],
        }
        context = self._context(repo_facts, {OWNER_ID: FAMILY_ID})
        update = self._update(OWNER_ID)

        await _build_orchestrator().agentic_memory(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Memory for devops (private)" in text
        assert f"Family memory for f{FAMILY_ID}:devops (shared)" in text
        assert "private rule" in text
        assert "family rule" in text
        # Private scope renders before the shared scope.
        assert text.index("(private)") < text.index("(shared)")

    async def test_no_family_user_sees_no_family_section(self):
        repo_facts = {"devops": [self._fact("private rule")]}
        context = self._context(repo_facts, {})
        update = self._update(OWNER_ID)

        await _build_orchestrator().agentic_memory(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Memory for devops (private)" in text
        assert "Family memory" not in text
