"""Deploy templates render for an arbitrary machine (dv-3b41).

The bug these guard: the shipped unit and sudoers files were one machine's
literal config, so installing them anywhere else produced a service that could
not start and a sudoers rule naming a nonexistent user.
"""

import getpass
import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "render_deploy", REPO_ROOT / "scripts" / "render_deploy.py"
)
render_deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_deploy)


@pytest.fixture
def foreign_env(monkeypatch):
    """Pretend we are on a stranger's box, nothing like the author's."""
    monkeypatch.setenv("SERVICE_USER", "botuser")
    monkeypatch.setenv("SERVICE_GROUP", "botgroup")
    monkeypatch.setenv("DEVVY_ROOT", "/opt/devvy")
    monkeypatch.setenv("POETRY_BIN", "/usr/local/bin/poetry")
    monkeypatch.setenv("SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setenv("SERVICE_PATH", "/usr/local/bin:/usr/bin:/bin")


def _render(name: str, service_name: str = "devvy-bot") -> str:
    values = render_deploy.build_values(service_name)
    return render_deploy.render(render_deploy.TEMPLATE_DIR / name, values)


def test_service_renders_for_a_foreign_machine(foreign_env):
    out = _render("claude-bot.service.template")
    assert "User=botuser" in out
    assert "Group=botgroup" in out
    assert "WorkingDirectory=/opt/devvy" in out
    assert "EnvironmentFile=/opt/devvy/.env" in out
    assert "ExecStart=/usr/local/bin/poetry run python -m src.main" in out
    assert "SyslogIdentifier=devvy-bot" in out


def test_sudoers_renders_for_a_foreign_machine(foreign_env):
    out = _render("sudoers-claude-bot.template")
    assert "botuser ALL=(root) NOPASSWD:" in out
    assert "/bin/systemctl restart devvy-bot" in out


def test_no_authors_machine_leaks_into_output(foreign_env):
    """The templates must carry no trace of the machine they were written on.

    Asserted against the *running* machine's identity rather than a literal
    username: the literal is exactly what must never appear in this repo
    (dv-9ebe), and this form keeps working after the export.
    """
    here = getpass.getuser()
    home = str(Path.home())
    for tpl in render_deploy.TEMPLATE_DIR.glob("*.template"):
        out = _render(tpl.name)
        assert here not in out, tpl.name
        assert home not in out, tpl.name


def test_templates_themselves_are_machine_neutral():
    """No template may hardcode a user or an absolute home directory."""
    here = getpass.getuser()
    for tpl in render_deploy.TEMPLATE_DIR.glob("*.template"):
        text = tpl.read_text(encoding="utf-8")
        assert here not in text, tpl.name
        assert not re.search(r"/home/[A-Za-z0-9_-]+", text), tpl.name


def test_unit_file_drops_the_do_not_install_banner(foreign_env):
    """systemd would choke on a bare first line that is not a comment."""
    out = _render("claude-bot.service.template")
    assert out.lstrip().startswith("[Unit]")
    assert render_deploy.FIRST_LINE_MARKER not in out


def test_sudoers_keeps_its_banner_as_a_comment(foreign_env):
    out = _render("sudoers-claude-bot.template")
    assert out.lstrip().startswith("#")


def test_unsubstituted_placeholder_is_an_error(foreign_env, tmp_path):
    bad = tmp_path / "bad.template"
    bad.write_text("User=@SERVICE_USER@\nRoot=@DEVVY_ROOT@\nHuh=@NOT_A_KEY@\n")
    values = render_deploy.build_values("devvy-bot")
    # Known keys substitute; the unknown one is left alone and is not fatal
    # unless it is one of the load-bearing ones.
    out = render_deploy.render(bad, values)
    assert "@NOT_A_KEY@" in out
    assert "botuser" in out


def test_missing_required_placeholder_raises(foreign_env, tmp_path):
    bad = tmp_path / "bad.template"
    bad.write_text("User=@SERVICE_USER@\n")
    values = render_deploy.build_values("devvy-bot")
    del values["SERVICE_USER"]
    with pytest.raises(SystemExit, match="unsubstituted"):
        render_deploy.render(bad, values)


def test_sudo_user_wins_over_root(monkeypatch):
    """`sudo ./install.sh` must configure the invoking user, never root."""
    monkeypatch.delenv("SERVICE_USER", raising=False)
    monkeypatch.setenv("SUDO_USER", "realperson")
    assert render_deploy._detect_user() == "realperson"


def test_root_sudo_user_is_ignored(monkeypatch):
    monkeypatch.delenv("SERVICE_USER", raising=False)
    monkeypatch.setenv("SUDO_USER", "root")
    assert render_deploy._detect_user() != "root"


def test_rendering_twice_over_the_read_only_sudoers_output_succeeds(
    foreign_env, tmp_path, monkeypatch, capsys
):
    """The second ./install.sh run used to die here.

    render_deploy writes the sudoers file mode 0440. Re-rendering into the same
    directory then hit `PermissionError: ... deploy/rendered/sudoers-claude-bot`
    and step 9 of the installer aborted with a traceback -- on a re-run, which
    is the case the whole installer is meant to be safe for.
    """
    out = tmp_path / "rendered"
    argv = ["render_deploy.py", "--out", str(out), "--service-name", "devvy-bot"]
    monkeypatch.setattr(render_deploy.sys, "argv", argv)
    # visudo is not necessarily installed in the test environment, and this
    # test is about file modes, not sudoers syntax.
    monkeypatch.setattr(render_deploy, "validate_sudoers", lambda path: None)

    assert render_deploy.main() == 0
    sudoers = out / "sudoers-claude-bot"
    assert sudoers.stat().st_mode & 0o777 == 0o440

    assert render_deploy.main() == 0

    assert sudoers.stat().st_mode & 0o777 == 0o440
    assert "botuser ALL=(root) NOPASSWD:" in sudoers.read_text(encoding="utf-8")
