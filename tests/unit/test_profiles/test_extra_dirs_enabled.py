"""extra_dirs (dv-3103) and enabled (dv-ffb4) profile keys."""

from pathlib import Path

import pytest

from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    load_profile_registry,
    validate_registry_paths,
)

_BASE = """
profiles:
  - slug: devops
    name: DevOps
    working_dir: {root}/repo
    memory_dir: {root}/repo/memory
    persona: {root}/repo/agent.md
    default: true
    sandbox: false
"""


def _cfg(tmp_path: Path, extra: str) -> Path:
    p = tmp_path / "profiles.yaml"
    p.write_text(_BASE.format(root=tmp_path) + extra.format(root=tmp_path))
    return p


def _def(slug: str, **kw) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        **kw,
    )


# --- extra_dirs -------------------------------------------------------------


def test_extra_dirs_parsed_and_resolved(tmp_path):
    cfg = _cfg(
        tmp_path,
        """
  - slug: study
    name: Study
    working_dir: {root}/ws/study
    memory_dir: {root}/ws/study/memory
    persona: {root}/ws/study/persona.md
    extra_dirs:
      - {root}/notes
      - ${{DEVVY_ROOT}}/../other-project
""",
    )
    reg = load_profile_registry(cfg)
    study = reg.get_by_slug("study")
    # ${DEVVY_ROOT} defaults to the config file's grandparent; the `..` climbs once more.
    assert study.extra_dirs == (
        (tmp_path / "notes").resolve(),
        (tmp_path.parent.parent / "other-project").resolve(),
    )
    assert reg.get_by_slug("devops").extra_dirs == ()


def test_extra_dirs_refused_on_guest_profile(tmp_path):
    cfg = _cfg(
        tmp_path,
        """
  - slug: study
    name: Study
    working_dir: {root}/ws/study
    memory_dir: {root}/ws/study/memory
    persona: {root}/ws/study/persona.md
    permission_policy:
      allowed_roles: [owner, guest]
    extra_dirs: [{root}/notes]
""",
    )
    with pytest.raises(ValueError, match="guest-allowed"):
        load_profile_registry(cfg)


@pytest.mark.parametrize("bad", ["/", "~"])
def test_extra_dirs_refuses_root_and_home(tmp_path, bad):
    cfg = _cfg(
        tmp_path,
        f"""
  - slug: study
    name: Study
    working_dir: {{root}}/ws/study
    memory_dir: {{root}}/ws/study/memory
    persona: {{root}}/ws/study/persona.md
    extra_dirs: ["{bad}"]
""",
    )
    with pytest.raises(ValueError, match="too broad"):
        load_profile_registry(cfg)


def test_extra_dirs_may_not_overlap_another_profile(tmp_path):
    # The bot's own repo is devops' working_dir: granting it to a sandboxed
    # profile would be `sandbox: false` through the back door.
    cfg = _cfg(
        tmp_path,
        """
  - slug: study
    name: Study
    working_dir: {root}/ws/study
    memory_dir: {root}/ws/study/memory
    persona: {root}/ws/study/persona.md
    extra_dirs: [{root}/repo/src]
""",
    )
    with pytest.raises(ValueError, match="overlaps profile 'devops' working_dir"):
        load_profile_registry(cfg)


def test_extra_dirs_must_exist_and_be_writable(tmp_path):
    for d in ("repo", "ws/study"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "repo/agent.md").write_text("x")
    (tmp_path / "ws/study/persona.md").write_text("x")
    cfg = _cfg(
        tmp_path,
        """
  - slug: study
    name: Study
    working_dir: {root}/ws/study
    memory_dir: {root}/ws/study/memory
    persona: {root}/ws/study/persona.md
    extra_dirs: [{root}/notes]
""",
    )
    reg = load_profile_registry(cfg)
    with pytest.raises(ValueError, match="extra_dirs entry is not a directory"):
        validate_registry_paths(reg)
    (tmp_path / "notes").mkdir()
    validate_registry_paths(reg)  # now fine


# --- enabled ----------------------------------------------------------------


def test_enabled_defaults_true_and_filters(tmp_path):
    cfg = _cfg(
        tmp_path,
        """
  - slug: pm
    name: PM
    working_dir: {root}/ws/pm
    memory_dir: {root}/ws/pm/memory
    persona: {root}/ws/pm/persona.md
    enabled: false
  - slug: general
    name: General
    working_dir: {root}/ws/general
    memory_dir: {root}/ws/general/memory
    persona: {root}/ws/general/persona.md
    permission_policy:
      allowed_roles: [owner, guest]
""",
    )
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("devops").enabled is True
    assert reg.get_by_slug("pm").enabled is False
    assert {p.slug for p in reg.profiles} == {"devops", "pm", "general"}
    assert {p.slug for p in reg.enabled_profiles} == {"devops", "general"}
    # Disabled stays resolvable for state (memory scopes, workspaces) ...
    assert reg.get_by_slug("pm") is not None
    assert reg.get_by_working_dir(tmp_path / "ws/pm").slug == "pm"
    # ... but is not a switch target.
    assert reg.get_enabled_by_slug("pm") is None
    assert reg.get_enabled_by_slug("general").slug == "general"


def test_enabled_rejects_non_bool(tmp_path):
    cfg = _cfg(
        tmp_path,
        """
  - slug: pm
    name: PM
    working_dir: {root}/ws/pm
    memory_dir: {root}/ws/pm/memory
    persona: {root}/ws/pm/persona.md
    enabled: "no"
""",
    )
    with pytest.raises(ValueError, match="enabled must be true or false"):
        load_profile_registry(cfg)


def test_default_cannot_be_disabled(tmp_path):
    p = tmp_path / "profiles.yaml"
    p.write_text(
        _BASE.format(root=tmp_path).replace(
            "default: true", "default: true\n    enabled: false"
        )
    )
    with pytest.raises(ValueError, match="default and cannot be disabled"):
        load_profile_registry(p)


def test_guest_default_skips_disabled_general():
    reg = ProfileRegistry(
        [
            _def("devops", default=True, sandbox=False),
            _def("general", allowed_roles=frozenset({"guest"}), enabled=False),
            _def("chef", allowed_roles=frozenset({"guest"})),
        ]
    )
    assert reg.get_default_for_role("guest").slug == "chef"
    assert reg.get_default_for_role("owner").slug == "devops"
