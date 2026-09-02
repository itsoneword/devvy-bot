"""Profile path resolution and boot-time validation (dv-3b41).

Covers the two halves separately: resolution is a pure function of the YAML and
the config file's location, validation is an opt-in filesystem check.
"""

import os
from pathlib import Path

import pytest

from src.profiles.registry import ROOT_VAR, load_profile_registry

PROFILE_TMPL = """
profiles:
  - slug: devops
    name: DevOps
    working_dir: {working_dir}
    memory_dir: {memory_dir}
    persona: {persona}
    default: true
"""


def _write_cfg(tmp_path: Path, **paths: str) -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    cfg = cfg_dir / "profiles.yaml"
    cfg.write_text(PROFILE_TMPL.format(**paths), encoding="utf-8")
    return cfg


def _materialise(root: Path) -> None:
    """Create a working_dir/memory_dir/persona trio under ``root``."""
    (root / "workspace" / "memory").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "persona.md").write_text("persona", encoding="utf-8")


# --- resolution ---------------------------------------------------------


def test_absolute_paths_pass_through_unchanged(tmp_path):
    """Existing installs must not break."""
    cfg = _write_cfg(
        tmp_path,
        working_dir="/srv/bot",
        memory_dir="/srv/bot/memory",
        persona="/srv/bot/agent.md",
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == Path("/srv/bot")
    assert p.memory_dir == Path("/srv/bot/memory")
    assert p.persona == Path("/srv/bot/agent.md")


def test_relative_paths_resolve_against_config_dir(tmp_path):
    """A relative path is relative to profiles.yaml, not the process CWD."""
    cfg = _write_cfg(
        tmp_path,
        working_dir="../workspace",
        memory_dir="../workspace/memory",
        persona="../workspace/persona.md",
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == tmp_path / "workspace"
    assert p.persona == tmp_path / "workspace" / "persona.md"


def test_relative_resolution_is_independent_of_cwd(tmp_path, monkeypatch):
    cfg = _write_cfg(
        tmp_path,
        working_dir="../workspace",
        memory_dir="../workspace/memory",
        persona="../workspace/persona.md",
    )
    monkeypatch.chdir("/")
    from_root = load_profile_registry(cfg).get_by_slug("devops").working_dir
    monkeypatch.chdir(tmp_path)
    from_tmp = load_profile_registry(cfg).get_by_slug("devops").working_dir
    assert from_root == from_tmp == tmp_path / "workspace"


def test_devvy_root_defaults_to_config_grandparent(tmp_path):
    """<root>/config/profiles.yaml -> ${DEVVY_ROOT} == <root>, unexported."""
    cfg = _write_cfg(
        tmp_path,
        working_dir="${DEVVY_ROOT}/workspace",
        memory_dir="${DEVVY_ROOT}/workspace/memory",
        persona="${DEVVY_ROOT}/workspace/persona.md",
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == tmp_path / "workspace"


def test_devvy_root_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(ROOT_VAR, "/opt/devvy")
    cfg = _write_cfg(
        tmp_path,
        working_dir="${DEVVY_ROOT}/workspace",
        memory_dir="${DEVVY_ROOT}/workspace/memory",
        persona="${DEVVY_ROOT}/workspace/persona.md",
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == Path("/opt/devvy/workspace")


def test_arbitrary_env_var_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACES", "/mnt/ws")
    cfg = _write_cfg(
        tmp_path,
        working_dir="${WORKSPACES}/coach",
        memory_dir="${WORKSPACES}/coach/memory",
        persona="${WORKSPACES}/coach/persona.md",
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == Path("/mnt/ws/coach")


def test_undefined_variable_is_an_error_not_an_empty_string(tmp_path, monkeypatch):
    """${UNSET}/coach must not silently become /coach."""
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    cfg = _write_cfg(
        tmp_path,
        working_dir="${NOPE_NOT_SET}/coach",
        memory_dir="/tmp/m",
        persona="/tmp/p.md",
    )
    with pytest.raises(ValueError) as exc:
        load_profile_registry(cfg)
    msg = str(exc.value)
    assert "NOPE_NOT_SET" in msg
    assert "devops" in msg


def test_tilde_expands(tmp_path):
    cfg = _write_cfg(
        tmp_path,
        working_dir="~/botdir",
        memory_dir="~/botdir/memory",
        persona="~/botdir/persona.md",
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == Path.home() / "botdir"


def test_resolution_is_lexical_not_symlink_following(tmp_path):
    """normpath, not resolve(): routing matches on the configured path."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    cfg = _write_cfg(
        tmp_path,
        working_dir=str(link),
        memory_dir=str(link / "memory"),
        persona=str(link / "persona.md"),
    )
    p = load_profile_registry(cfg).get_by_slug("devops")
    assert p.working_dir == link


# --- validation ---------------------------------------------------------


def test_validation_is_off_by_default(tmp_path):
    """Parsing stays pure; nonexistent paths load fine without the flag."""
    cfg = _write_cfg(
        tmp_path,
        working_dir="/nonexistent/nowhere",
        memory_dir="/nonexistent/nowhere/memory",
        persona="/nonexistent/nowhere/persona.md",
    )
    assert load_profile_registry(cfg).get_by_slug("devops") is not None


def test_validation_passes_on_a_real_tree(tmp_path):
    _materialise(tmp_path)
    cfg = _write_cfg(
        tmp_path,
        working_dir="../workspace",
        memory_dir="../workspace/memory",
        persona="../workspace/persona.md",
    )
    reg = load_profile_registry(cfg, validate_paths=True)
    assert reg.get_by_slug("devops").slug == "devops"


def test_missing_persona_names_profile_and_path(tmp_path):
    _materialise(tmp_path)
    (tmp_path / "workspace" / "persona.md").unlink()
    cfg = _write_cfg(
        tmp_path,
        working_dir="../workspace",
        memory_dir="../workspace/memory",
        persona="../workspace/persona.md",
    )
    with pytest.raises(ValueError) as exc:
        load_profile_registry(cfg, validate_paths=True)
    msg = str(exc.value)
    assert "devops" in msg
    assert "persona" in msg
    assert str(tmp_path / "workspace" / "persona.md") in msg


def test_missing_working_dir_names_profile_and_path(tmp_path):
    _materialise(tmp_path)
    cfg = _write_cfg(
        tmp_path,
        working_dir="../gone",
        memory_dir="../workspace/memory",
        persona="../workspace/persona.md",
    )
    with pytest.raises(ValueError) as exc:
        load_profile_registry(cfg, validate_paths=True)
    msg = str(exc.value)
    assert "devops" in msg
    assert "working_dir" in msg
    assert str(tmp_path / "gone") in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores write permissions")
def test_unwritable_working_dir_is_rejected(tmp_path):
    _materialise(tmp_path)
    ws = tmp_path / "workspace"
    ws.chmod(0o555)
    cfg = _write_cfg(
        tmp_path,
        working_dir="../workspace",
        memory_dir="../workspace/memory",
        persona="../workspace/persona.md",
    )
    try:
        with pytest.raises(ValueError, match="not writable"):
            load_profile_registry(cfg, validate_paths=True)
    finally:
        ws.chmod(0o755)


def test_absent_memory_dir_is_allowed_when_creatable(tmp_path):
    """bootstrap materialises memory_dir on first run -- absence is not fatal."""
    _materialise(tmp_path)
    cfg = _write_cfg(
        tmp_path,
        working_dir="../workspace",
        memory_dir="../workspace/not_yet/memory",
        persona="../workspace/persona.md",
    )
    reg = load_profile_registry(cfg, validate_paths=True)
    assert reg.get_by_slug("devops").memory_dir.name == "memory"


def test_missing_mcp_file_is_rejected(tmp_path):
    _materialise(tmp_path)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    cfg = cfg_dir / "profiles.yaml"
    cfg.write_text(
        """
profiles:
  - slug: devops
    name: DevOps
    working_dir: ../workspace
    memory_dir: ../workspace/memory
    persona: ../workspace/persona.md
    mcp: ../workspace/absent-mcp.json
    default: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mcp config does not exist"):
        load_profile_registry(cfg, validate_paths=True)
