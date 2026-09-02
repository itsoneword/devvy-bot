"""First-run profile bootstrap (dv-3b41, criterion 3).

The contract is narrow and worth pinning exactly: create what's missing, seed
personas from templates, and never touch anything that already exists — because
the same code runs on every re-install over live data.
"""

from pathlib import Path

import pytest

from src.boot.profiles import bootstrap_profiles
from src.profiles import load_profile_registry, validate_registry_paths

CFG = """
profiles:
  - slug: devops
    name: DevOps
    working_dir: ../ws/devops
    memory_dir: ../ws/devops/memory
    persona: ../ws/devops/persona.md
    persona_template: ../templates/agent.md
    default: true
  - slug: general
    name: General
    working_dir: ../ws/general
    memory_dir: ../ws/general/memory
    persona: ../ws/general/persona.md
"""


def _write_cfg(tmp_path: Path, body: str = CFG) -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    cfg = cfg_dir / "profiles.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _write_templates(tmp_path: Path) -> Path:
    """Templates root with one explicit template and one by convention."""
    templates = tmp_path / "templates"
    (templates / "personas").mkdir(parents=True, exist_ok=True)
    (templates / "agent.md").write_text("# devops persona\n", encoding="utf-8")
    (templates / "personas" / "general.md").write_text(
        "# general persona\n", encoding="utf-8"
    )
    return templates


async def test_first_run_materialises_dirs_and_personas(tmp_path):
    """A clone with nothing under ws/ boots into a complete tree."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, templates)

    for slug, expected in (
        ("devops", "# devops persona\n"),
        ("general", "# general persona\n"),
    ):
        p = registry.get_by_slug(slug)
        assert p.working_dir.is_dir()
        assert p.memory_dir.is_dir()
        assert p.persona.read_text(encoding="utf-8") == expected

    # And the tree it produced passes the boot check it runs before.
    validate_registry_paths(registry)


async def test_convention_template_is_personas_slug_md(tmp_path):
    """No YAML key needed: <templates>/personas/<slug>.md is found by name."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, templates)

    general = registry.get_by_slug("general")
    assert general.persona_template is None
    assert general.persona.read_text(encoding="utf-8") == "# general persona\n"


async def test_explicit_persona_template_wins_over_convention(tmp_path):
    """devops names its template; a same-slug convention file is ignored."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)
    (templates / "personas" / "devops.md").write_text("decoy\n", encoding="utf-8")
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, templates)

    assert (
        registry.get_by_slug("devops").persona.read_text(encoding="utf-8")
        == "# devops persona\n"
    )


async def test_rerun_never_overwrites_an_existing_persona(tmp_path):
    """The install is idempotent over live data — this is the whole point."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)
    registry = load_profile_registry(cfg)
    general = registry.get_by_slug("general")
    general.persona.parent.mkdir(parents=True, exist_ok=True)
    general.persona.write_text("hand-edited, must survive\n", encoding="utf-8")
    (general.memory_dir).mkdir(parents=True, exist_ok=True)
    (general.memory_dir / "notes.md").write_text("user data\n", encoding="utf-8")

    await bootstrap_profiles(registry, templates)
    await bootstrap_profiles(registry, templates)

    assert general.persona.read_text(encoding="utf-8") == "hand-edited, must survive\n"
    assert (general.memory_dir / "notes.md").read_text(
        encoding="utf-8"
    ) == "user data\n"


async def test_missing_template_is_left_for_validation_to_report(tmp_path):
    """Bootstrap stays silent-ish; validate_registry_paths names the profile."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)
    (templates / "personas" / "general.md").unlink()
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, templates)

    general = registry.get_by_slug("general")
    assert general.working_dir.is_dir()  # dirs still created
    assert not general.persona.exists()
    with pytest.raises(ValueError, match="general.*persona file does not exist"):
        validate_registry_paths(registry)


async def test_absent_templates_dir_still_creates_directories(tmp_path):
    """A stripped-down install without data/templates must not crash boot."""
    cfg = _write_cfg(tmp_path)
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, tmp_path / "nope")

    assert registry.get_by_slug("general").working_dir.is_dir()
    assert not registry.get_by_slug("general").persona.exists()


async def test_file_where_a_directory_belongs_is_left_alone(tmp_path):
    """Don't mkdir over it — validation reports it more usefully."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)
    registry = load_profile_registry(cfg)
    general = registry.get_by_slug("general")
    general.working_dir.parent.mkdir(parents=True, exist_ok=True)
    general.working_dir.write_text("not a directory", encoding="utf-8")

    await bootstrap_profiles(registry, templates)

    assert general.working_dir.is_file()
    with pytest.raises(ValueError, match="working_dir is not a directory"):
        validate_registry_paths(registry)


async def test_profile_template_dir_layout_is_found(tmp_path):
    """dv-4e29 layout: <templates>/profiles/<slug>/persona.md, next to the
    profile.yaml fragment the installer instantiated the profile from."""
    cfg = _write_cfg(tmp_path)
    templates = tmp_path / "templates"
    (templates / "profiles" / "general").mkdir(parents=True, exist_ok=True)
    (templates / "agent.md").write_text("# devops persona\n", encoding="utf-8")
    (templates / "profiles" / "general" / "persona.md").write_text(
        "# general from the profile template dir\n", encoding="utf-8"
    )
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, templates)

    general = registry.get_by_slug("general")
    assert (
        general.persona.read_text(encoding="utf-8")
        == "# general from the profile template dir\n"
    )
    validate_registry_paths(registry)


async def test_profile_dir_layout_wins_over_the_flat_personas_dir(tmp_path):
    """Both layouts present: the per-profile directory is the current one."""
    cfg = _write_cfg(tmp_path)
    templates = _write_templates(tmp_path)  # writes personas/general.md
    (templates / "profiles" / "general").mkdir(parents=True, exist_ok=True)
    (templates / "profiles" / "general" / "persona.md").write_text(
        "# newer layout\n", encoding="utf-8"
    )
    registry = load_profile_registry(cfg)

    await bootstrap_profiles(registry, templates)

    assert (
        registry.get_by_slug("general").persona.read_text(encoding="utf-8")
        == "# newer layout\n"
    )
