"""Profile instantiation from templates (dv-4e29, folded into dv-7d92).

The bugs these guard, in order of how much they would cost:

1. A shipped template with a YAML typo -- the installer writes a file, the
   service refuses to boot, and the message is a parser error in the journal.
   ``test_every_shipped_template_loads`` renders each one and puts it through
   the real ``load_profile_registry``.
2. A guest-allowed profile that resolves to ``sandbox: false`` -- a guest with
   full bash network and cross-filesystem access.
3. A second install run duplicating entries or clobbering a config the owner
   has since edited.
"""

import getpass
import importlib.util
from pathlib import Path

import pytest

from src.profiles import load_profile_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "install_profiles", REPO_ROOT / "scripts" / "install_profiles.py"
)
install_profiles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_profiles)

TEMPLATES = REPO_ROOT / "data" / "templates" / "profiles"


def _write_config(tmp_path: Path, text: str) -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "profiles.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


# --- the shipped templates ---------------------------------------------


def test_default_profiles_are_devops_and_general():
    """dv-1a55 D4. If this changes, it is a product decision, not a refactor."""
    assert install_profiles.DEFAULT_PROFILES == ("devops", "general")


def test_every_shipped_template_is_complete():
    names = install_profiles.available_templates(TEMPLATES)
    assert {"devops", "general", "chef", "coach", "pm", "channel"} <= set(names)
    for name in names:
        if name == "devops":
            # Reuses data/templates/agent.md via an explicit persona_template.
            continue
        assert (TEMPLATES / name / "persona.md").is_file(), name


@pytest.mark.parametrize(
    "name", sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir())
)
def test_every_shipped_template_loads(name, tmp_path, monkeypatch):
    """Render one template and load it with the real registry loader.

    Always alongside devops: the registry requires exactly one `default: true`
    profile, and devops is the one that carries it.
    """
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    names = ["devops"] if name == "devops" else ["devops", name]
    text = install_profiles.build_config(names, TEMPLATES)
    cfg = _write_config(tmp_path, text)

    registry = load_profile_registry(cfg)

    assert [p.slug for p in registry.profiles] == names
    profile = registry.get_by_slug(name)
    assert str(profile.working_dir).startswith(str(tmp_path))
    assert "@SLUG@" not in text
    assert getpass.getuser() not in text


def test_default_install_loads_and_has_exactly_one_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    text = install_profiles.build_config(install_profiles.DEFAULT_PROFILES, TEMPLATES)
    cfg = _write_config(tmp_path, text)

    registry = load_profile_registry(cfg)

    assert [p.slug for p in registry.profiles] == ["devops", "general"]
    assert registry.get_default().slug == "devops"


def test_no_template_carries_an_absolute_home_path():
    """A hardcoded /home/... is what made the old tracked profiles.yaml
    useless on every machine but one."""
    for path in TEMPLATES.glob("*/profile.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text, path
        assert getpass.getuser() not in text, path


# --- rendering ----------------------------------------------------------


def test_slug_substitution_reaches_paths_and_slug():
    out = install_profiles.render_fragment(
        '- slug: "@SLUG@"\n  working_dir: ${DEVVY_ROOT}/workspaces/@SLUG@\n', "work"
    )
    assert '- slug: "work"' in out
    assert "workspaces/work" in out
    assert "@SLUG@" not in out


def test_fragment_is_indented_to_sit_under_profiles_key():
    out = install_profiles.render_fragment('- slug: "@SLUG@"\n  name: X\n', "x")
    assert out.startswith("  - slug:")
    assert "\n    name: X" in out


def test_devvy_root_is_left_for_the_registry_to_expand():
    out = install_profiles.render_fragment(
        '- slug: "@SLUG@"\n  working_dir: ${DEVVY_ROOT}/x\n', "x"
    )
    assert "${DEVVY_ROOT}/x" in out


@pytest.mark.parametrize("slug", ["", "1bad", "Bad", "with space", "../escape"])
def test_a_bad_slug_is_refused(slug):
    with pytest.raises(install_profiles.TemplateError):
        install_profiles.render_fragment('- slug: "@SLUG@"\n', slug)


def test_unknown_template_names_what_is_available():
    with pytest.raises(install_profiles.TemplateError, match="no such profile"):
        install_profiles.read_template("nope", TEMPLATES)


# --- the safety gate ----------------------------------------------------


def test_guest_allowed_plus_sandbox_false_is_refused():
    fragment = install_profiles.render_fragment(
        '- slug: "@SLUG@"\n'
        "  sandbox: false\n"
        "  permission_policy:\n"
        "    allowed_roles: [owner, guest]\n",
        "bad",
    )
    with pytest.raises(install_profiles.TemplateError, match="allow guests"):
        install_profiles.assert_installable(fragment, "bad")


def test_owner_only_may_disable_the_sandbox():
    """devops is exactly this shape; refusing it would break the default."""
    fragment = install_profiles.render_fragment(
        '- slug: "@SLUG@"\n  sandbox: false\n', "devops"
    )
    install_profiles.assert_installable(fragment, "devops")


# --- idempotency --------------------------------------------------------


def test_installed_slugs_ignores_commented_out_entries():
    text = (
        "profiles:\n"
        "  - slug: devops\n"
        "  # - slug: channel\n"
        '  - slug: "general"\n'
    )
    assert install_profiles.installed_slugs(text) == ["devops", "general"]


def test_add_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    cfg = _write_config(
        tmp_path, install_profiles.build_config(["devops", "general"], TEMPLATES)
    )
    args = ["--config", str(cfg), "--templates", str(TEMPLATES), "--add", "coach"]

    assert install_profiles.main(args) == 0
    first = cfg.read_text(encoding="utf-8")
    assert install_profiles.main(args) == 0
    second = cfg.read_text(encoding="utf-8")

    assert first == second
    assert install_profiles.installed_slugs(second).count("coach") == 1
    assert load_profile_registry(cfg).get_by_slug("coach") is not None


def test_add_under_a_different_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    cfg = _write_config(tmp_path, install_profiles.build_config(["devops"], TEMPLATES))

    rc = install_profiles.main(
        [
            "--config",
            str(cfg),
            "--templates",
            str(TEMPLATES),
            "--add",
            "pm",
            "--slug",
            "work",
        ]
    )

    assert rc == 0
    registry = load_profile_registry(cfg)
    assert {p.slug for p in registry.profiles} == {"devops", "work"}
    assert registry.get_by_slug("work").working_dir.name == "work"


def test_rerunning_install_over_an_edited_config_keeps_the_edits(tmp_path, monkeypatch):
    """The re-run case that must never lose data: the owner has hand-edited
    profiles.yaml, then runs ./install.sh again."""
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    cfg = _write_config(tmp_path, install_profiles.build_config(["devops"], TEMPLATES))
    edited = cfg.read_text(encoding="utf-8").replace("max_turns: 100", "max_turns: 7")
    cfg.write_text(edited, encoding="utf-8")

    rc = install_profiles.main(
        [
            "--config",
            str(cfg),
            "--templates",
            str(TEMPLATES),
            "--install",
            "devops,general",
        ]
    )

    assert rc == 0
    after = cfg.read_text(encoding="utf-8")
    assert "max_turns: 7" in after
    registry = load_profile_registry(cfg)
    assert [p.slug for p in registry.profiles] == ["devops", "general"]
    assert registry.get_by_slug("devops").max_turns == 7


def test_install_backs_up_before_it_changes_an_existing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    cfg = _write_config(tmp_path, install_profiles.build_config(["devops"], TEMPLATES))
    before = cfg.read_text(encoding="utf-8")

    install_profiles.main(
        [
            "--config",
            str(cfg),
            "--templates",
            str(TEMPLATES),
            "--install",
            "devops,general",
        ]
    )

    backups = list(cfg.parent.glob("profiles.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == before


def test_dry_run_writes_nothing(tmp_path, capsys):
    cfg = tmp_path / "config" / "profiles.yaml"

    rc = install_profiles.main(
        [
            "--config",
            str(cfg),
            "--templates",
            str(TEMPLATES),
            "--install",
            "devops,general",
            "--dry-run",
        ]
    )

    assert rc == 0
    assert not cfg.exists()
    assert "slug" in capsys.readouterr().out
