from pathlib import Path

import pytest

from src.profiles.registry import (
    DEFAULT_AGENT_ID,
    ProfileDefinition,
    ProfileRegistry,
    agent_id_for_working_dir,
    load_profile_registry,
)


def _def(slug: str, default: bool = False) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug.title(),
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        default=default,
    )


def test_get_by_slug_and_default():
    reg = ProfileRegistry([_def("devops", default=True), _def("channel")])
    assert reg.get_by_slug("channel").name == "Channel"
    assert reg.get_default().slug == "devops"
    assert reg.get_by_slug("missing") is None
    assert {p.slug for p in reg.profiles} == {"devops", "channel"}


def test_get_by_working_dir():
    reg = ProfileRegistry([_def("devops", default=True), _def("channel")])
    assert reg.get_by_working_dir(Path("/tmp/channel")).slug == "channel"
    assert reg.get_by_working_dir(Path("/tmp/nope")) is None


def test_agent_id_for_working_dir_shared_resolver():
    """Single source of truth for read-path + write-path agent_id scoping.

    Guards against write/read divergence: the orchestrator (read) and the
    SDK/Codex backends (write) must resolve the same slug for a directory.
    """
    reg = ProfileRegistry([_def("devops", default=True), _def("chef")])
    # Match resolves to the profile slug; accepts both Path and str inputs.
    assert agent_id_for_working_dir(reg, Path("/tmp/chef")) == "chef"
    assert agent_id_for_working_dir(reg, "/tmp/chef") == "chef"
    # No match and no registry both fall back to the default scope.
    assert agent_id_for_working_dir(reg, "/tmp/unknown") == DEFAULT_AGENT_ID
    assert agent_id_for_working_dir(None, "/tmp/chef") == DEFAULT_AGENT_ID
    assert DEFAULT_AGENT_ID == "devops"


def _write(tmp_path, text):
    p = tmp_path / "profiles.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_valid(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
  - slug: channel
    name: Channel
    working_dir: /tmp/channel
    memory_dir: /tmp/channel/memory
    persona: /tmp/channel/persona.md
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_default().slug == "devops"
    assert reg.get_by_slug("channel").working_dir == Path("/tmp/channel")


def test_load_rejects_duplicate_slug(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: a
    name: A
    working_dir: /tmp/a
    memory_dir: /tmp/a/m
    persona: /tmp/a/p.md
    default: true
  - slug: a
    name: A2
    working_dir: /tmp/a2
    memory_dir: /tmp/a2/m
    persona: /tmp/a2/p.md
""",
    )
    with pytest.raises(ValueError, match="duplicate slug"):
        load_profile_registry(cfg)


def test_max_turns_defaults_none():
    assert _def("devops").max_turns is None


def test_manager_resolves_per_profile_max_turns():
    """Per-profile max_turns overrides the global CLAUDE_MAX_TURNS; profiles
    without one (and unknown dirs) inherit the global default."""
    import dataclasses

    from src.claude.sdk_integration import ClaudeSDKManager
    from src.config import create_test_config

    devops = dataclasses.replace(_def("devops", default=True), max_turns=100)
    reg = ProfileRegistry([devops, _def("chef")])
    config = create_test_config(claude_max_turns=30)
    mgr = ClaudeSDKManager(config, profile_registry=reg)
    assert mgr._max_turns_for(Path("/tmp/devops")) == 100
    assert mgr._max_turns_for(Path("/tmp/chef")) == 30
    assert mgr._max_turns_for(Path("/tmp/unknown")) == 30


def test_load_parses_max_turns_override(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
    max_turns: 100
  - slug: coach
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/memory
    persona: /tmp/coach/agent.md
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("devops").max_turns == 100
    assert reg.get_by_slug("coach").max_turns is None


def test_load_rejects_invalid_max_turns(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
    max_turns: 0
""",
    )
    with pytest.raises(ValueError, match="max_turns"):
        load_profile_registry(cfg)


def test_max_cost_per_request_defaults_none():
    assert _def("devops").max_cost_per_request is None


def test_load_parses_max_cost_per_request(tmp_path):
    """Cost caps belong in profile config next to max_turns, not only in env —
    a cheap agent and the admin agent want different ceilings."""
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
  - slug: coach
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/memory
    persona: /tmp/coach/agent.md
    max_cost_per_request: 1.5
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("coach").max_cost_per_request == 1.5
    assert reg.get_by_slug("devops").max_cost_per_request is None


def test_load_rejects_negative_max_cost_per_request(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
    max_cost_per_request: -1
""",
    )
    with pytest.raises(ValueError, match="max_cost_per_request"):
        load_profile_registry(cfg)


def test_family_memory_defaults_true():
    assert _def("devops").family_memory is True


def test_load_parses_family_memory_opt_out(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
  - slug: coach
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/memory
    persona: /tmp/coach/agent.md
    family_memory: false
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("devops").family_memory is True
    assert reg.get_by_slug("coach").family_memory is False


def test_personal_memory_defaults_true():
    """Per-user is the default (dv-0fbb); sharing is the explicit opt-in."""
    assert _def("devops").personal_memory is True


def test_load_parses_personal_memory_explicit(tmp_path):
    """Explicit `personal_memory` keys still parse; a bare profile defaults
    to per-user."""
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
    personal_memory: false
  - slug: coach
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/memory
    persona: /tmp/coach/agent.md
    personal_memory: true
  - slug: general
    name: General
    working_dir: /tmp/general
    memory_dir: /tmp/general/memory
    persona: /tmp/general/agent.md
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("devops").personal_memory is False
    assert reg.get_by_slug("coach").personal_memory is True
    assert reg.get_by_slug("general").personal_memory is True


def test_load_parses_shared_opt_out(tmp_path):
    """`shared: true` is the readable opt-out spelling — it maps to
    personal_memory=False; `shared: false` keeps the per-user default."""
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: chef
    name: Chef
    working_dir: /tmp/chef
    memory_dir: /tmp/chef/memory
    persona: /tmp/chef/agent.md
    default: true
    shared: true
  - slug: general
    name: General
    working_dir: /tmp/general
    memory_dir: /tmp/general/memory
    persona: /tmp/general/agent.md
    shared: false
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("chef").personal_memory is False
    assert reg.get_by_slug("general").personal_memory is True


def test_load_rejects_shared_and_personal_memory_together(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: chef
    name: Chef
    working_dir: /tmp/chef
    memory_dir: /tmp/chef/memory
    persona: /tmp/chef/agent.md
    default: true
    shared: true
    personal_memory: false
""",
    )
    with pytest.raises(ValueError, match="set either 'shared' or 'personal_memory'"):
        load_profile_registry(cfg)


def test_load_production_shape_resolves_intended_scopes(tmp_path):
    """A profiles.yaml shaped like production: devops/chef/pm opt into
    sharing, coach declares per-user explicitly, general/channel are bare
    and get the per-user default."""
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
    shared: true
  - slug: chef
    name: Chef
    working_dir: /tmp/chef
    memory_dir: /tmp/chef/memory
    persona: /tmp/chef/agent.md
    shared: true
  - slug: pm
    name: PM
    working_dir: /tmp/pm
    memory_dir: /tmp/pm/memory
    persona: /tmp/pm/agent.md
    shared: true
  - slug: coach
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/memory
    persona: /tmp/coach/agent.md
    personal_memory: true
  - slug: general
    name: General
    working_dir: /tmp/general
    memory_dir: /tmp/general/memory
    persona: /tmp/general/agent.md
  - slug: channel
    name: Channel
    working_dir: /tmp/channel
    memory_dir: /tmp/channel/memory
    persona: /tmp/channel/agent.md
""",
    )
    reg = load_profile_registry(cfg)
    shared = {"devops", "chef", "pm"}
    per_user = {"coach", "general", "channel"}
    for slug in shared:
        assert reg.get_by_slug(slug).personal_memory is False, slug
    for slug in per_user:
        assert reg.get_by_slug(slug).personal_memory is True, slug


class TestAgentIdForWorkingDirPersonalMemory:
    """personal_memory: true must split the PRIVATE scope per user, even
    for two "owner"-role users sharing the same profile working_dir --
    the bug this guards: two co-owner family members (e.g. both spouses)
    otherwise resolve to the exact same agent_id and see each other's
    private facts."""

    def _reg(self, *, personal_memory: bool) -> ProfileRegistry:
        profile = ProfileDefinition(
            slug="coach",
            name="Coach",
            working_dir=Path("/tmp/coach"),
            memory_dir=Path("/tmp/coach/memory"),
            persona=Path("/tmp/coach/agent.md"),
            default=True,
            personal_memory=personal_memory,
        )
        return ProfileRegistry([profile])

    def test_two_owners_get_distinct_scopes_when_opted_in(self):
        reg = self._reg(personal_memory=True)
        husband = agent_id_for_working_dir(reg, Path("/tmp/coach"), 111)
        wife = agent_id_for_working_dir(reg, Path("/tmp/coach"), 222)
        assert husband != wife
        assert husband == "coach#u111"
        assert wife == "coach#u222"

    def test_owners_share_one_scope_when_opted_out(self):
        """Shared behavior (personal_memory: false, i.e. `shared: true` in
        YAML) is unchanged: both resolve to the bare profile slug."""
        reg = self._reg(personal_memory=False)
        husband = agent_id_for_working_dir(reg, Path("/tmp/coach"), 111)
        wife = agent_id_for_working_dir(reg, Path("/tmp/coach"), 222)
        assert husband == wife == "coach"

    def test_no_user_id_falls_back_to_bare_slug_even_when_opted_in(self):
        """Callers that can't resolve a user_id (e.g. no update.effective_user)
        keep getting the bare slug -- only an explicit user_id splits scope."""
        reg = self._reg(personal_memory=True)
        assert agent_id_for_working_dir(reg, Path("/tmp/coach")) == "coach"


def test_load_requires_exactly_one_default(tmp_path):
    cfg = _write(
        tmp_path,
        """
profiles:
  - slug: a
    name: A
    working_dir: /tmp/a
    memory_dir: /tmp/a/m
    persona: /tmp/a/p.md
""",
    )
    with pytest.raises(ValueError, match="exactly one default"):
        load_profile_registry(cfg)
