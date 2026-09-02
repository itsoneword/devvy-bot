import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, CodexSessionStore
from src.config.settings import Settings


@pytest.fixture
def settings():
    return Settings(telegram_bot_token="x", telegram_bot_username="x")


def test_build_argv_adds_image_flags(settings, tmp_path):
    integ = CodexCliIntegration(
        config=settings, session_store=CodexSessionStore(tmp_path / "s.json")
    )
    img = tmp_path / "pic.png"
    img.write_bytes(b"x")
    argv = integ._build_argv(
        model="openai-codex/gpt-5.4-mini",
        cwd=tmp_path,
        out_file=tmp_path / "o.txt",
        resume_id=None,
        images=[img],
    )
    assert "-i" in argv
    assert str(img) in argv


def test_build_argv_no_images_has_no_i_flag(settings, tmp_path):
    integ = CodexCliIntegration(
        config=settings, session_store=CodexSessionStore(tmp_path / "s.json")
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.4-mini",
        cwd=tmp_path,
        out_file=tmp_path / "o.txt",
        resume_id=None,
        images=None,
    )
    assert "-i" not in argv


def test_build_argv_resume_places_subcommand_after_exec_options(settings, tmp_path):
    # `codex exec resume` rejects exec-level flags (e.g. -C) as its own args,
    # so the resume subcommand must come AFTER all exec options.
    integ = CodexCliIntegration(
        config=settings, session_store=CodexSessionStore(tmp_path / "s.json")
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.4-mini",
        cwd=tmp_path,
        out_file=tmp_path / "o.txt",
        resume_id="abc-123",
        images=None,
    )
    assert "resume" in argv and "abc-123" in argv
    # resume subcommand + id are last (prompt is appended later by run_command)
    assert argv[-2:] == ["resume", "abc-123"]
    # every exec-level flag precedes `resume`
    ri = argv.index("resume")
    for flag in ("-C", "-m", "-s", "-o", "--json", "--skip-git-repo-check"):
        assert argv.index(flag) < ri
