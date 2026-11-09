"""Pinned/core fact changes re-freeze the current window (dv-ef59).

Amends dv-5e2a: general facts stay frozen until the roll, but a pinned or core
add / correct / forget binds the same session on its next turn.
"""

import json

import pytest

from src.claude.prompt_composer import PromptComposer
from src.claude.window_bundle import WindowBundleProvider
from src.evaluator.writer import MemoryWriter
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository
from src.storage.window_bundle_repository import WindowBundleRepository
from src.storage.window_repository import WindowRepository


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "refresh.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
def env(db, tmp_path):
    persona = tmp_path / "agent.md"
    persona.write_text("You are the test agent persona.", encoding="utf-8")
    memory_repo = MemoryRepository(db)
    bundle_repo = WindowBundleRepository(db)
    provider = WindowBundleProvider(
        window_repo=WindowRepository(db),
        bundle_repo=bundle_repo,
        memory_repo=memory_repo,
        profile_registry=None,
        default_composer=PromptComposer(memory_path=tmp_path, agent_md_path=persona),
    )
    writer = MemoryWriter(memory_repo, source="agent_tool", bundle_repo=bundle_repo)
    return provider, writer, memory_repo, tmp_path / "ws"


async def _add(writer, tier, content, agent_id="devops", **extra):
    res = await writer.apply_ops(
        agent_id, [{"op": "add", "tier": tier, "content": content, **extra}]
    )
    assert res.added == 1
    return res.outcomes[0].fact_id


@pytest.mark.parametrize("tier", ["pinned", "core"])
async def test_pinned_or_core_add_refreshes_bundle(env, tier):
    provider, writer, _repo, wd = env
    first = await provider.get_or_materialize(wd, user_id=1)
    assert first is not None and not first.reused

    await _add(writer, tier, "never use the word anxious")

    second = await provider.get_or_materialize(wd, user_id=1)
    assert second.reused is False
    assert second.window_id == first.window_id
    assert "never use the word anxious" in second.text
    assert second.sha256 != first.sha256

    third = await provider.get_or_materialize(wd, user_id=1)
    assert third.reused is True
    assert third.sha256 == second.sha256


async def test_pinned_forget_refreshes_bundle(env):
    provider, writer, _repo, wd = env
    fid = await _add(writer, "pinned", "never mention the cat")
    first = await provider.get_or_materialize(wd, user_id=1)
    assert "never mention the cat" in first.text

    res = await writer.apply_ops(
        "devops", [{"op": "invalidate", "target_id": fid, "scope": "private"}]
    )
    assert res.invalidated == 1

    second = await provider.get_or_materialize(wd, user_id=1)
    assert second.reused is False
    assert "never mention the cat" not in second.text


async def test_pinned_correct_refreshes_bundle(env):
    provider, writer, _repo, wd = env
    fid = await _add(writer, "pinned", "call me Bob")
    first = await provider.get_or_materialize(wd, user_id=1)
    assert "call me Bob" in first.text

    res = await writer.apply_ops(
        "devops",
        [
            {
                "op": "supersede",
                "supersedes_id": fid,
                "tier": "pinned",
                "content": "call me Robert",
            }
        ],
    )
    assert res.superseded == 1

    second = await provider.get_or_materialize(wd, user_id=1)
    assert second.reused is False
    assert "call me Robert" in second.text
    assert "call me Bob" not in second.text


async def test_general_changes_stay_frozen(env):
    provider, writer, _repo, wd = env
    gid = await _add(writer, "general", "likes tea")
    first = await provider.get_or_materialize(wd, user_id=1)

    await _add(writer, "general", "switched to coffee")
    await writer.apply_ops(
        "devops",
        [
            {
                "op": "supersede",
                "supersedes_id": gid,
                "tier": "general",
                "content": "likes green tea",
            }
        ],
    )
    await writer.apply_ops(
        "devops", [{"op": "invalidate", "target_id": gid, "scope": "private"}]
    )

    again = await provider.get_or_materialize(wd, user_id=1)
    assert again.reused is True
    assert again.sha256 == first.sha256
    assert "switched to coffee" not in again.text


async def test_other_agents_pinned_write_does_not_refresh(env):
    provider, writer, _repo, wd = env
    first = await provider.get_or_materialize(wd, user_id=1)
    await _add(writer, "pinned", "someone else's rule", agent_id="other")
    again = await provider.get_or_materialize(wd, user_id=1)
    assert again.reused is True
    assert again.sha256 == first.sha256


async def test_family_pinned_write_refreshes_members_only(env):
    provider, writer, _repo, wd = env
    a = await provider.get_or_materialize(wd / "a", user_id=1, family_id=7)
    b = await provider.get_or_materialize(wd / "b", user_id=2, family_id=7)
    loner = await provider.get_or_materialize(wd / "c", user_id=3)
    assert a and b and loner

    res = await writer.apply_ops(
        "devops",
        [
            {
                "op": "add",
                "tier": "pinned",
                "content": "household: no screens at dinner",
                "scope": "family",
            }
        ],
        family_agent_id="f7:devops",
    )
    assert res.added == 1

    a2 = await provider.get_or_materialize(wd / "a", user_id=1, family_id=7)
    b2 = await provider.get_or_materialize(wd / "b", user_id=2, family_id=7)
    loner2 = await provider.get_or_materialize(wd / "c", user_id=3)
    assert not a2.reused and "no screens at dinner" in a2.text
    assert not b2.reused and "no screens at dinner" in b2.text
    assert loner2.reused is True
    assert loner2.sha256 == loner.sha256


# --- remember / memory_forget / memory_correct tool results (AC 7, 8) -------


def _payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def tools(env):
    from src.claude.tool_context import AgentServices, TurnContext

    _provider, writer, repo, _wd = env
    audit = MemoryWriter(repo, source="user_request", bundle_repo=writer._bundle_repo)
    services = AgentServices(
        event_bus=None,
        task_repo=None,
        memory_writer=writer,
        memory_repo=repo,
        audit_memory_writer=audit,
    )
    return services, TurnContext(profile_slug="devops", memory_agent_id="devops")


@pytest.mark.parametrize(
    "tier,effective",
    [("pinned", "next_turn"), ("core", "next_turn"), ("general", "next_window")],
)
async def test_remember_reports_effective_from(tools, tier, effective):
    from src.claude.agent_tools import remember

    services, ctx = tools
    out = _payload(
        await remember(services, ctx, {"fact": f"a {tier} fact", "tier": tier})
    )
    assert out["status"] == "saved"
    assert out["effective"] == effective
    assert out["detail"]


async def test_forget_and_correct_report_effective_from(tools, env):
    from src.claude.agent_tools import memory_correct, memory_forget

    services, ctx = tools
    _p, writer, _r, _w = env
    pinned = await _add(writer, "pinned", "pinned thing")
    general = await _add(writer, "general", "general thing")

    out = _payload(
        await memory_correct(
            services, ctx, {"fact_id": pinned, "new_fact": "pinned thing v2"}
        )
    )
    assert out["status"] == "corrected"
    assert out["effective"] == "next_turn"

    out = _payload(await memory_forget(services, ctx, {"fact_id": general}))
    assert out["status"] == "forgotten"
    assert out["effective"] == "next_window"


def test_remember_spec_does_not_promise_live_general_facts():
    from src.claude.agent_tools import ALL_TOOL_SPECS

    spec = next(s for s in ALL_TOOL_SPECS if s.name == "remember")
    assert "next window" in spec.description.lower()
