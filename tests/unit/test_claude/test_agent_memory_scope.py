"""Server-side memory-scope derivation for the in-session `remember` tool.

dv-9c24. The tool's whole safety argument is that the scope it writes to is
computed HERE, from the session's working dir + user id + family id, and baked
into the tool closure — the model has no way to name a store. These tests pin
that derivation; `tests/unit/test_claude/test_agent_tools.py::TestRemember`
pins the tool's behaviour given those values.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.claude import agent_tools
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry


def _profile(
    slug: str, root: Path, *, personal_memory: bool = False, default: bool = False
) -> ProfileDefinition:
    root.mkdir(parents=True, exist_ok=True)
    return ProfileDefinition(
        slug=slug,
        name=slug.capitalize(),
        working_dir=root,
        memory_dir=root / "memory",
        persona=root / "agent.md",
        default=default,
        personal_memory=personal_memory,
    )


@pytest.fixture
def manager(tmp_path: Path) -> ClaudeSDKManager:
    registry = ProfileRegistry(
        [
            _profile("coach", tmp_path / "coach", personal_memory=True),
            _profile("devops", tmp_path / "devops", default=True),
        ]
    )
    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=2,
        enable_mcp=False,
    )
    mgr = ClaudeSDKManager(config, profile_registry=registry)
    mgr.attach_agent_services(
        agent_tools.AgentServices(
            event_bus=object(), task_repo=object(), memory_writer=object()
        )
    )
    return mgr


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Record the kwargs `_agent_mcp_server_for` hands the builder."""
    calls: List[Dict[str, Any]] = []

    def fake_build(*args: Any, **kwargs: Any) -> Dict[str, str]:
        calls.append(kwargs)
        return {"stub": "server"}

    monkeypatch.setattr(agent_tools, "build_agent_mcp_server", fake_build)
    return calls


def _scopes(
    mgr: ClaudeSDKManager,
    calls: List[Dict[str, Any]],
    working_dir: Path,
    user_id: Optional[int] = None,
    family_id: Optional[int] = None,
) -> Dict[str, Any]:
    mgr._agent_mcp_server_for(
        working_dir, requester_chat_id=1, user_id=user_id, family_id=family_id
    )
    return calls[-1]


def test_personal_memory_profile_scopes_per_user(manager, captured, tmp_path):
    kwargs = _scopes(manager, captured, tmp_path / "coach", user_id=111)
    assert kwargs["memory_agent_id"] == "coach#u111"
    assert kwargs["memory_family_agent_id"] is None


def test_two_users_on_one_profile_never_share_a_scope(manager, captured, tmp_path):
    """The property the tool relies on: scope is a function of the session."""
    a = _scopes(manager, captured, tmp_path / "coach", user_id=111)
    b = _scopes(manager, captured, tmp_path / "coach", user_id=222)
    assert a["memory_agent_id"] != b["memory_agent_id"]


def test_guest_workspace_scopes_from_the_path_alone(manager, captured, tmp_path):
    """Guest tier works with no extra plumbing and no widened boundary."""
    guest_dir = tmp_path / "devops" / "guests" / "333"
    guest_dir.mkdir(parents=True)
    kwargs = _scopes(manager, captured, guest_dir)
    assert kwargs["memory_agent_id"] == "devops#u333"


def test_family_scope_is_resolved_only_when_the_session_has_a_family(
    manager, captured, tmp_path
):
    with_family = _scopes(manager, captured, tmp_path / "coach", 111, family_id=7)
    assert with_family["memory_family_agent_id"] == "f7:coach"

    without = _scopes(manager, captured, tmp_path / "coach", 111)
    assert without["memory_family_agent_id"] is None


def test_scope_matches_the_one_prompt_composition_reads(manager, captured, tmp_path):
    """Write path and read path must resolve identically, or facts vanish."""
    kwargs = _scopes(manager, captured, tmp_path / "coach", user_id=111, family_id=7)
    assert kwargs["memory_agent_id"] == manager._agent_id_for(tmp_path / "coach", 111)
    assert kwargs["memory_family_agent_id"] == manager._family_agent_id_for(
        tmp_path / "coach", 7, 111
    )


def test_role_reaches_the_builder(manager, captured, tmp_path):
    """dv-a900: the job-tool role gate lives in the builder; the manager must
    hand it the resolved role verbatim."""
    manager._agent_mcp_server_for(tmp_path / "coach", user_id=111, role="guest")
    assert captured[-1]["role"] == "guest"

    manager._agent_mcp_server_for(tmp_path / "coach", user_id=111, role="owner")
    assert captured[-1]["role"] == "owner"


def test_role_defaults_to_none_fail_closed(manager, captured, tmp_path):
    """Callers that never resolve a role must not accidentally grant tools."""
    manager._agent_mcp_server_for(tmp_path / "coach", user_id=111)
    assert captured[-1]["role"] is None


def test_no_server_mounted_without_agent_services(tmp_path, captured):
    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        enable_mcp=False,
    )
    mgr = ClaudeSDKManager(config)
    assert mgr._agent_mcp_server_for(tmp_path, user_id=111) == {}
    assert captured == []
