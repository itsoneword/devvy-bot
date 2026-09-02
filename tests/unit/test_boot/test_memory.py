"""Tests for boot.memory bootstrap module."""

from pathlib import Path

import pytest

from src.boot.memory import bootstrap_memory, bootstrap_project_stubs


@pytest.fixture()
def templates_dir(tmp_path: Path) -> Path:
    """Create a templates directory with soul.md and memory files."""
    tpl = tmp_path / "templates"
    tpl.mkdir()

    # soul.md template
    (tpl / "soul.md").write_text("# Soul\nI am Atlas.\n")

    # agent.md and user.md templates
    (tpl / "agent.md").write_text("# Agent Identity\n\n**Name:** Atlas\n")
    (tpl / "user.md").write_text("# User Profile\n\nPrimary languages: Python\n")

    # memory templates
    mem = tpl / "memory"
    mem.mkdir()
    (mem / "index.md").write_text("# Memory Index\n")
    (mem / "projects.md").write_text("# Projects\n")
    (mem / "user_notes.md").write_text("# User Notes\n")
    (mem / "activity.md").write_text("# Activity\n")
    (mem / "focus.md").write_text("# Focus\n\nRolling context\n")
    (mem / "decisions.md").write_text("# Decisions\n")
    (mem / "preferences.md").write_text("# Preferences\n")

    sessions = mem / "sessions"
    sessions.mkdir()
    (sessions / "example-project.md").write_text("# Example\n")

    return tpl


class TestBootstrapMemoryCreatesFiles:
    """bootstrap_memory creates missing files from templates."""

    async def test_creates_soul_md_from_template(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert soul_path.exists()
        assert "Atlas" in soul_path.read_text()

    async def test_creates_all_memory_files(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert (memory_path / "index.md").exists()
        assert (memory_path / "projects.md").exists()
        assert (memory_path / "user_notes.md").exists()
        assert (memory_path / "activity.md").exists()

    async def test_creates_subdirectories(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert (memory_path / "sessions" / "example-project.md").exists()


class TestBootstrapMemoryPreservesExisting:
    """bootstrap_memory does NOT overwrite existing files."""

    async def test_does_not_overwrite_existing_soul_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        soul_path.parent.mkdir(parents=True, exist_ok=True)
        soul_path.write_text("# My Custom Soul\n")

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert soul_path.read_text() == "# My Custom Soul\n"

    async def test_does_not_overwrite_existing_memory_files(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        memory_path.mkdir(parents=True, exist_ok=True)
        (memory_path / "index.md").write_text("# Custom Index\n")

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert (memory_path / "index.md").read_text() == "# Custom Index\n"
        # But other files should be created
        assert (memory_path / "projects.md").exists()


class TestBootstrapMemoryPartialState:
    """bootstrap_memory fills gaps in partial state."""

    async def test_fills_gaps_when_some_files_exist(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        # Pre-create soul and index only
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        soul_path.write_text("# Existing Soul\n")
        memory_path.mkdir(parents=True, exist_ok=True)
        (memory_path / "index.md").write_text("# Existing Index\n")

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        # Existing files unchanged
        assert soul_path.read_text() == "# Existing Soul\n"
        assert (memory_path / "index.md").read_text() == "# Existing Index\n"
        # Missing files created
        assert (memory_path / "projects.md").exists()
        assert (memory_path / "activity.md").exists()
        assert (memory_path / "sessions" / "example-project.md").exists()


class TestBootstrapMemoryGracefulDegradation:
    """bootstrap_memory handles missing templates gracefully."""

    async def test_handles_missing_templates_dir(self, tmp_path: Path) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"
        nonexistent = tmp_path / "nonexistent"

        # Should not raise
        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=nonexistent,
        )

        assert not soul_path.exists()
        assert not memory_path.exists()

    async def test_handles_missing_soul_template(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        # Remove the soul.md template
        (templates_dir / "soul.md").unlink()

        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        # Should not raise; memory files should still be created
        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert not soul_path.exists()
        # Memory files should still be created
        assert (memory_path / "index.md").exists()


class TestBootstrapAgentAndUserMd:
    """bootstrap_memory provisions agent.md and user.md alongside soul.md."""

    async def test_bootstrap_creates_agent_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        agent_path = soul_path.parent / "agent.md"
        assert agent_path.exists()
        assert "Atlas" in agent_path.read_text()

    async def test_bootstrap_creates_user_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        user_path = soul_path.parent / "user.md"
        assert user_path.exists()
        assert "User Profile" in user_path.read_text()

    async def test_bootstrap_does_not_overwrite_agent_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        agent_path = soul_path.parent / "agent.md"
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text("# Custom Agent\n")

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert agent_path.read_text() == "# Custom Agent\n"

    async def test_bootstrap_creates_focus_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert (memory_path / "focus.md").exists()
        assert "Focus" in (memory_path / "focus.md").read_text()

    async def test_bootstrap_creates_decisions_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert (memory_path / "decisions.md").exists()
        assert "Decisions" in (memory_path / "decisions.md").read_text()

    async def test_bootstrap_creates_preferences_md(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        soul_path = tmp_path / "runtime" / "soul.md"
        memory_path = tmp_path / "runtime" / "memory"

        await bootstrap_memory(
            memory_path=memory_path,
            soul_md_path=soul_path,
            templates_dir=templates_dir,
        )

        assert (memory_path / "preferences.md").exists()
        assert "Preferences" in (memory_path / "preferences.md").read_text()


class TestBootstrapProjectStubs:
    """Tests for bootstrap_project_stubs function."""

    async def test_creates_stub_from_claude_md(self, tmp_path: Path) -> None:
        """Stub created with CLAUDE.md excerpt when project dir has one."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text(
            "# My Project\n\nSome overview content.", encoding="utf-8"
        )

        config_path = tmp_path / "projects.yaml"
        config_path.write_text(
            "projects:\n  - slug: my-project\n    directory: "
            + str(project_dir)
            + "\n",
            encoding="utf-8",
        )

        memory_path = tmp_path / "memory"
        memory_path.mkdir()

        await bootstrap_project_stubs(
            memory_path=memory_path, projects_config_path=config_path
        )

        stub = memory_path / "projects" / "my-project.md"
        assert stub.exists()
        content = stub.read_text(encoding="utf-8")
        assert "# my-project" in content
        assert str(project_dir) in content
        assert "Some overview content" in content

    async def test_does_not_overwrite_existing_stub(self, tmp_path: Path) -> None:
        """Existing stubs are never overwritten."""
        project_dir = tmp_path / "existing-proj"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("New content", encoding="utf-8")

        config_path = tmp_path / "projects.yaml"
        config_path.write_text(
            "projects:\n  - slug: existing-proj\n    directory: "
            + str(project_dir)
            + "\n",
            encoding="utf-8",
        )

        memory_path = tmp_path / "memory"
        projects_dir = memory_path / "projects"
        projects_dir.mkdir(parents=True)
        stub = projects_dir / "existing-proj.md"
        stub.write_text("Original content", encoding="utf-8")

        await bootstrap_project_stubs(
            memory_path=memory_path, projects_config_path=config_path
        )

        assert stub.read_text(encoding="utf-8") == "Original content"

    async def test_creates_stub_without_claude_md(self, tmp_path: Path) -> None:
        """Stub created even when project has no CLAUDE.md."""
        project_dir = tmp_path / "bare-project"
        project_dir.mkdir()

        config_path = tmp_path / "projects.yaml"
        config_path.write_text(
            "projects:\n  - slug: bare-project\n    directory: "
            + str(project_dir)
            + "\n",
            encoding="utf-8",
        )

        memory_path = tmp_path / "memory"
        memory_path.mkdir()

        await bootstrap_project_stubs(
            memory_path=memory_path, projects_config_path=config_path
        )

        stub = memory_path / "projects" / "bare-project.md"
        assert stub.exists()
        content = stub.read_text(encoding="utf-8")
        assert "# bare-project" in content
        assert "From CLAUDE.md" not in content

    async def test_skips_when_no_config(self, tmp_path: Path) -> None:
        """No error when projects config path is None."""
        memory_path = tmp_path / "memory"
        memory_path.mkdir()
        await bootstrap_project_stubs(
            memory_path=memory_path, projects_config_path=None
        )
        assert not (memory_path / "projects").exists()

    async def test_skips_when_config_missing(self, tmp_path: Path) -> None:
        """No error when projects config file doesn't exist."""
        memory_path = tmp_path / "memory"
        memory_path.mkdir()
        await bootstrap_project_stubs(
            memory_path=memory_path,
            projects_config_path=tmp_path / "nonexistent.yaml",
        )
        assert not (memory_path / "projects").exists()

    async def test_truncates_long_claude_md(self, tmp_path: Path) -> None:
        """CLAUDE.md excerpt truncated at ~2000 chars."""
        project_dir = tmp_path / "big-project"
        project_dir.mkdir()
        long_content = "# Big\n\n" + "x" * 3000
        (project_dir / "CLAUDE.md").write_text(long_content, encoding="utf-8")

        config_path = tmp_path / "projects.yaml"
        config_path.write_text(
            "projects:\n  - slug: big-project\n    directory: "
            + str(project_dir)
            + "\n",
            encoding="utf-8",
        )

        memory_path = tmp_path / "memory"
        memory_path.mkdir()

        await bootstrap_project_stubs(
            memory_path=memory_path, projects_config_path=config_path
        )

        stub = memory_path / "projects" / "big-project.md"
        content = stub.read_text(encoding="utf-8")
        assert "From CLAUDE.md" in content
        # Full 3000-char line should NOT appear
        assert "x" * 3000 not in content
