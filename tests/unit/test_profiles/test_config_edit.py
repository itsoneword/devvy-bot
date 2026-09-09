"""Comment-preserving enabled: splice for profiles.yaml (dv-ffb4)."""

from pathlib import Path

import pytest

from src.profiles.config_edit import render_profile_enabled, set_profile_enabled
from src.profiles.registry import load_profile_registry

SAMPLE = """\
# Profile registry for THIS machine.
profiles:
  - slug: devops              # default
    name: Server / DevOps
    working_dir: /tmp/repo
    memory_dir: /tmp/repo/memory
    persona: /tmp/repo/agent.md
    default: true
    sandbox: false

  - slug: pm
    name: Surf Business PM
    working_dir: /tmp/pm
    memory_dir: /tmp/pm/memory
    persona: /tmp/pm/persona.md
    permission_policy:
      allowed_roles: [owner, guest]
    # family_memory defaults to true
    shared: true              # deferred to dv-6761

  - slug: "coach"
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/memory
    persona: /tmp/coach/persona.md
    enabled: true   # explicit
"""


def test_disable_inserts_after_slug_line_and_keeps_comments():
    out = render_profile_enabled(SAMPLE, "pm", False)
    assert "  - slug: pm\n    enabled: false\n    name: Surf Business PM\n" in out
    # Untouched elsewhere, comments intact.
    assert "# family_memory defaults to true" in out
    assert "# deferred to dv-6761" in out
    assert "# default" in out
    assert out.count("enabled:") == 2  # pm + coach's explicit one


def test_enable_removes_the_key_when_absent_is_default():
    disabled = render_profile_enabled(SAMPLE, "pm", False)
    assert render_profile_enabled(disabled, "pm", True) == SAMPLE


def test_rewrite_existing_line_keeps_trailing_comment():
    out = render_profile_enabled(SAMPLE, "coach", False)
    assert "    enabled: false   # explicit\n" in out
    assert "enabled: true" not in out


def test_enable_already_enabled_is_noop():
    assert render_profile_enabled(SAMPLE, "pm", True) == SAMPLE


def test_unknown_slug_raises():
    with pytest.raises(ValueError, match="not found"):
        render_profile_enabled(SAMPLE, "nope", False)


def test_set_profile_enabled_validates_before_write(tmp_path: Path):
    cfg = tmp_path / "config" / "profiles.yaml"
    cfg.parent.mkdir()
    cfg.write_text(SAMPLE)
    # Disabling the default is refused by the loader -> file untouched.
    with pytest.raises(ValueError, match="cannot be disabled"):
        set_profile_enabled(cfg, "devops", False)
    assert cfg.read_text() == SAMPLE
    assert list(cfg.parent.iterdir()) == [cfg]  # no temp file left behind

    set_profile_enabled(cfg, "pm", False)
    reg = load_profile_registry(cfg)
    assert reg.get_by_slug("pm").enabled is False
    assert [p.slug for p in reg.enabled_profiles] == ["devops", "coach"]
    assert list(cfg.parent.iterdir()) == [cfg]
