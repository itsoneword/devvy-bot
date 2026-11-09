"""Boot-time degrade: a profile with bad paths is off for this run (dv-86f4 p2)."""

from pathlib import Path

import pytest

from src.bot.orchestration.agents_menu import build_agents_manage
from src.profiles.registry import load_profile_registry, validate_registry_paths

YAML = """
profiles:
{blocks}
"""

BLOCK = """  - slug: {slug}
    name: {name}
    working_dir: {root}/{slug}
    memory_dir: {root}/{slug}/memory
    persona: {root}/{slug}/persona.md
{default}{extra}"""


def _make(tmp_path: Path, *, devops_extra=None, gymbro_extra=None):
    blocks = []
    for slug, extra, default in (
        ("devops", devops_extra, True),
        ("gymbro", gymbro_extra, False),
        ("chef", None, False),
    ):
        d = tmp_path / slug
        (d / "memory").mkdir(parents=True)
        (d / "persona.md").write_text("p", encoding="utf-8")
        ex = f"    extra_dirs: [{extra}]\n" if extra else ""
        blocks.append(
            BLOCK.format(
                slug=slug,
                name=slug.title(),
                root=tmp_path,
                default="    default: true\n" if default else "",
                extra=ex,
            )
        )
    cfg = tmp_path / "profiles.yaml"
    cfg.write_text(YAML.format(blocks="".join(blocks)), encoding="utf-8")
    return load_profile_registry(cfg, data_root=None)


def test_degrade_disables_only_the_broken_profile(tmp_path):
    missing = tmp_path / "gymbro-staging"
    reg = _make(tmp_path, gymbro_extra=missing)
    reasons = validate_registry_paths(reg, degrade=True)
    assert set(reasons) == {"gymbro"}
    assert str(missing) in reasons["gymbro"]
    assert reg.get_by_slug("gymbro").enabled is False
    assert reg.get_enabled_by_slug("gymbro") is None
    assert reg.boot_disabled["gymbro"] == reasons["gymbro"]


def test_other_profiles_keep_working_while_one_is_off(tmp_path):
    reg = _make(tmp_path, gymbro_extra=tmp_path / "nope")
    validate_registry_paths(reg, degrade=True)
    assert {p.slug for p in reg.enabled_profiles} == {"devops", "chef"}
    assert reg.get_enabled_by_slug("chef") is not None
    assert reg.get_by_working_dir(tmp_path / "chef").slug == "chef"
    assert reg.get_default().slug == "devops"


def test_degrade_with_valid_config_changes_nothing(tmp_path):
    reg = _make(tmp_path)
    assert validate_registry_paths(reg, degrade=True) == {}
    assert len(reg.enabled_profiles) == 3
    assert not reg.boot_disabled


def test_default_profile_broken_is_still_fatal(tmp_path):
    reg = _make(tmp_path, devops_extra=tmp_path / "nope")
    with pytest.raises(ValueError, match="devops"):
        validate_registry_paths(reg, degrade=True)


def test_strict_validation_stays_strict(tmp_path):
    missing = tmp_path / "gymbro-staging"
    reg = _make(tmp_path, gymbro_extra=missing)
    with pytest.raises(ValueError) as e:
        validate_registry_paths(reg)
    assert "gymbro" in str(e.value) and str(missing) in str(e.value)
    assert reg.get_by_slug("gymbro").enabled is True


def test_manage_view_marks_profile_off_this_run_with_reason(tmp_path):
    missing = tmp_path / "gymbro-staging"
    reg = _make(tmp_path, gymbro_extra=missing)
    validate_registry_paths(reg, degrade=True)
    text, markup = build_agents_manage(reg, reg)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    gym = [t for t in labels if "gymbro" in t]
    assert gym and "off this run" in gym[0]
    assert "off this run" in text or "off this run" in gym[0]
    assert str(missing) in text + " ".join(labels)
    # healthy profile is not marked
    assert not any("chef" in t and "off this run" in t for t in labels)


def test_manage_view_has_no_mark_without_boot_disabled(tmp_path):
    reg = _make(tmp_path)
    text, markup = build_agents_manage(reg, reg)
    assert "off this run" not in text
    assert not any("off this run" in b.text for r in markup.inline_keyboard for b in r)


def test_main_boot_uses_degrade_and_tells_owner():
    src = (Path(__file__).parents[3] / "src" / "main.py").read_text("utf-8")
    assert "degrade=True" in src
    assert "выключен" in src


def test_check_install_stays_strict():
    from src.boot import check

    src = Path(check.__file__).read_text("utf-8")
    assert "degrade=True" not in src
