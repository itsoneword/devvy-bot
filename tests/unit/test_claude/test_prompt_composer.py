"""Tests for PromptComposer -- layered system prompt assembly."""

from pathlib import Path

import pytest

from src.claude.prompt_composer import PromptComposer


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Create a memory directory with focus.md."""
    mem = tmp_path / "memory"
    mem.mkdir()
    return mem


@pytest.fixture
def agent_md(tmp_path: Path) -> Path:
    """Create an agent.md file."""
    path = tmp_path / "agent.md"
    path.write_text(
        "# Agent Identity\n\n**Name:** Atlas\n"
        "Obsidian vault at /home/testuser/notes/",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def composer(memory_dir: Path, agent_md: Path) -> PromptComposer:
    return PromptComposer(memory_path=memory_dir, agent_md_path=agent_md)


class TestPromptComposer:
    """Test compose() builds structured prompt with XML-tagged sections."""

    def test_agent_identity_loaded(
        self, composer: PromptComposer, tmp_path: Path
    ) -> None:
        result = composer.compose(working_directory=tmp_path)
        assert "<agent_identity>" in result
        assert "**Name:** Atlas" in result
        assert "</agent_identity>" in result

    def test_obsidian_vault_in_agent(
        self, composer: PromptComposer, tmp_path: Path
    ) -> None:
        result = composer.compose(working_directory=tmp_path)
        assert "/home/testuser/notes/" in result

    def test_layered_composition_order(
        self, composer: PromptComposer, memory_dir: Path, tmp_path: Path
    ) -> None:
        result = composer.compose(
            working_directory=tmp_path,
            pinned_facts=["no oven"],
            general_facts=["loves beef stew"],
        )

        # Verify order: directory constraint < agent_identity < constraints
        #               < user_facts
        idx_dir = result.index("All file operations")
        idx_agent = result.index("<agent_identity>")
        idx_pinned = result.index("<constraints>")
        idx_general = result.index("<user_facts>")
        assert idx_dir < idx_agent < idx_pinned < idx_general

    def test_project_claude_md_is_not_a_layer(
        self, composer: PromptComposer, tmp_path: Path
    ) -> None:
        """CLAUDE.md is loaded by the CLI ("project" setting source), not here.

        Composing it in as well duplicated the whole file in context. Guard the
        regression: compose() must take no project-instructions input and must
        not emit that section.
        """
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md content", encoding="utf-8")

        result = composer.compose(working_directory=tmp_path)

        assert "<project_instructions>" not in result
        assert "# CLAUDE.md content" not in result
        with pytest.raises(TypeError):
            composer.compose(
                working_directory=tmp_path,
                project_claude_md="# CLAUDE.md content",  # type: ignore[call-arg]
            )

    def test_missing_files_skipped_gracefully(self, tmp_path: Path) -> None:
        """Composer works even when no memory files exist."""
        mem = tmp_path / "empty_memory"
        mem.mkdir()
        agent = tmp_path / "nonexistent_agent.md"
        c = PromptComposer(memory_path=mem, agent_md_path=agent)
        result = c.compose(working_directory=tmp_path)
        assert "All file operations" in result
        assert "<agent_identity>" not in result
        assert "<constraints>" not in result
        assert "<user_facts>" not in result

    def test_empty_files_skipped(self, tmp_path: Path) -> None:
        mem = tmp_path / "memory"
        mem.mkdir()
        agent = tmp_path / "agent.md"
        agent.write_text("   \n  ", encoding="utf-8")  # whitespace only
        c = PromptComposer(memory_path=mem, agent_md_path=agent)
        result = c.compose(working_directory=tmp_path)
        assert "<agent_identity>" not in result

    def test_directory_constraint_always_present(
        self, composer: PromptComposer
    ) -> None:
        result = composer.compose(working_directory=Path("/some/dir"))
        assert "All file operations must stay within /some/dir" in result


class TestComposeFacts:
    """Test compose() renders DB-backed pinned/general facts."""

    def test_compose_renders_pinned_and_general_facts(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "agent.md"
        agent_md.write_text("You are a chef.", encoding="utf-8")
        composer = PromptComposer(memory_path=tmp_path, agent_md_path=agent_md)

        prompt = composer.compose(
            working_directory=tmp_path,
            pinned_facts=["allergic to peanuts"],
            general_facts=["loves beef stew"],
        )

        assert "<constraints>" in prompt
        assert "allergic to peanuts" in prompt
        assert "<user_facts>" in prompt
        assert "loves beef stew" in prompt
        assert "<agent_identity>" in prompt
        # focus.md / recent_context is gone
        assert "<recent_context>" not in prompt

    def test_snapshot_header_wording(self, tmp_path: Path) -> None:
        """dv-b9c7 step 1: headers frame fact blocks as window-start snapshots.

        The old "Hard rules. Never violate these." header over a frozen
        snapshot was the direct mechanic behind stale-constraint nagging.
        """
        agent_md = tmp_path / "agent.md"
        agent_md.write_text("You are a chef.", encoding="utf-8")
        composer = PromptComposer(memory_path=tmp_path, agent_md_path=agent_md)

        prompt = composer.compose(
            working_directory=tmp_path,
            pinned_facts=["allergic to peanuts"],
            general_facts=["loves beef stew"],
        )

        # constraints: snapshot framing, conversation supersedes, explicit-only
        assert "Standing constraints — a snapshot" in prompt
        assert "the conversation wins" in prompt
        assert "never retire one by your own inference" in prompt
        # user_facts: snapshot framing, user wins on conflict
        assert "a snapshot frozen when this window started" in prompt
        assert "the user wins" in prompt
        # old wording must be gone
        assert "Hard rules" not in prompt
        assert "may be outdated" not in prompt

    def test_compose_omits_empty_fact_blocks(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "agent.md"
        agent_md.write_text("You are a chef.", encoding="utf-8")
        composer = PromptComposer(memory_path=tmp_path, agent_md_path=agent_md)

        prompt = composer.compose(working_directory=tmp_path)

        assert "<constraints>" not in prompt
        assert "<user_facts>" not in prompt


class TestGetSectionSizes:
    """Test get_section_sizes() returns char counts."""

    def test_returns_sizes_for_existing_files(
        self, composer: PromptComposer, memory_dir: Path, tmp_path: Path
    ) -> None:
        sizes = composer.get_section_sizes()
        # Only the persona file is file-backed now; facts live in the DB.
        assert "agent.md" in sizes
        assert sizes["agent.md"] > 0
        assert "user.md" not in sizes
        assert "focus.md" not in sizes

    def test_omits_missing_files(self, tmp_path: Path) -> None:
        mem = tmp_path / "memory"
        mem.mkdir()
        c = PromptComposer(memory_path=mem, agent_md_path=tmp_path / "nope.md")
        sizes = c.get_section_sizes()
        assert sizes == {}
