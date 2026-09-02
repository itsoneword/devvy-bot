"""Unit tests for ActivityLogger - LOG-01 through LOG-04."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.activity.logger import ActivityLogger

ACTIVITY_HEADER = """\
# Activity Log

> Auto-logged by the bot after each interaction. Do not edit manually.
> Format: timestamp | project | summary

| When | Project | Summary |
|------|---------|---------|
"""


@pytest.fixture()
def memory_dir(tmp_path: Path) -> Path:
    """Create a memory directory with an activity.md template."""
    activity_file = tmp_path / "activity.md"
    activity_file.write_text(ACTIVITY_HEADER, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def logger(memory_dir: Path) -> ActivityLogger:
    """Create an ActivityLogger pointed at tmp_path."""
    return ActivityLogger(memory_path=memory_dir, max_lines=200)


# ---------------------------------------------------------------------------
# TestActivityLoggerAppend
# ---------------------------------------------------------------------------


class TestActivityLoggerAppend:
    """log_activity() appends a markdown table row to activity.md."""

    async def test_appends_row_to_activity_file(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        await logger.log_activity(
            prompt="Hello world",
            response_content="Hi there",
            working_directory=Path("/home/user/my-project"),
            num_turns=1,
        )

        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        # Header is 7 lines, so we expect at least 8 lines total
        assert len(lines) >= 8
        # Last line should be a table row starting with |
        assert lines[-1].startswith("|")

    async def test_appends_multiple_rows(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        for i in range(3):
            await logger.log_activity(
                prompt=f"Prompt {i}",
                response_content=f"Response {i}",
                working_directory=Path("/home/user/proj"),
                num_turns=i + 1,
            )

        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        data_rows = [
            line
            for line in content.strip().splitlines()
            if line.startswith("|") and "---" not in line and "When" not in line
        ]
        assert len(data_rows) == 3


# ---------------------------------------------------------------------------
# TestActivityEntryFormat
# ---------------------------------------------------------------------------


class TestActivityEntryFormat:
    """Entry format: timestamp, project dir name, turn count, truncated snippets."""

    async def test_entry_contains_timestamp(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        await logger.log_activity(
            prompt="test",
            response_content="resp",
            working_directory=Path("/home/user/cool-project"),
            num_turns=2,
        )
        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        last_line = content.strip().splitlines()[-1]
        # Should contain a date like 2026-03-09 19:00
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", last_line)

    async def test_entry_contains_project_dir_name(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        await logger.log_activity(
            prompt="test",
            response_content="resp",
            working_directory=Path("/home/user/cool-project"),
            num_turns=2,
        )
        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        last_line = content.strip().splitlines()[-1]
        assert "cool-project" in last_line

    async def test_entry_contains_turn_count(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        await logger.log_activity(
            prompt="test",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=5,
        )
        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        last_line = content.strip().splitlines()[-1]
        assert "[5t]" in last_line

    async def test_truncate_collapses_whitespace_and_cuts(self) -> None:
        long_text = "a " * 200  # 400 chars with spaces
        result = ActivityLogger._truncate(long_text, max_len=100)
        assert len(result) <= 100
        assert result.endswith("...")
        # No double spaces in result
        assert "  " not in result

    async def test_truncate_short_text_unchanged(self) -> None:
        short = "hello world"
        result = ActivityLogger._truncate(short, max_len=100)
        assert result == "hello world"

    async def test_truncate_collapses_newlines(self) -> None:
        text = "line1\nline2\nline3"
        result = ActivityLogger._truncate(text, max_len=100)
        assert "\n" not in result
        assert result == "line1 line2 line3"

    async def test_entry_has_truncated_input_and_output(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        long_prompt = "x" * 200
        long_response = "y" * 200
        await logger.log_activity(
            prompt=long_prompt,
            response_content=long_response,
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )
        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        last_line = content.strip().splitlines()[-1]
        assert "..." in last_line
        assert "->" in last_line


# ---------------------------------------------------------------------------
# TestActivityLogRotation
# ---------------------------------------------------------------------------


class TestActivityLogRotation:
    """Rotation: when file exceeds max_lines, older entries move to archive."""

    def _make_entries(self, n: int) -> str:
        """Generate n data rows."""
        rows = []
        for i in range(n):
            rows.append(
                f"| 2026-03-0{(i % 9) + 1} 10:00 | proj-{i} | [{i}t] input -> output |"
            )
        return "\n".join(rows) + "\n"

    async def test_rotation_triggers_when_exceeding_max_lines(
        self, tmp_path: Path
    ) -> None:
        activity_file = tmp_path / "activity.md"
        # Header (7 lines) + 20 data rows = 27 lines, max_lines=20
        activity_file.write_text(
            ACTIVITY_HEADER + self._make_entries(20), encoding="utf-8"
        )
        logger = ActivityLogger(memory_path=tmp_path, max_lines=20)

        await logger.log_activity(
            prompt="new entry",
            response_content="response",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )

        content = activity_file.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        # Should be trimmed (not 28+ lines)
        assert len(lines) <= 20

    async def test_rotation_creates_archive_file(self, tmp_path: Path) -> None:
        activity_file = tmp_path / "activity.md"
        activity_file.write_text(
            ACTIVITY_HEADER + self._make_entries(20), encoding="utf-8"
        )
        logger = ActivityLogger(memory_path=tmp_path, max_lines=20)

        await logger.log_activity(
            prompt="new entry",
            response_content="response",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )

        archive_dir = tmp_path / "archive"
        assert archive_dir.exists()
        archive_files = list(archive_dir.glob("activity-*.md"))
        assert len(archive_files) >= 1

    async def test_same_day_rotation_appends_to_existing_archive(
        self, tmp_path: Path
    ) -> None:
        activity_file = tmp_path / "activity.md"
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        archive_file = archive_dir / f"activity-{today}.md"
        archive_file.write_text("| old | archived | entry |\n", encoding="utf-8")

        activity_file.write_text(
            ACTIVITY_HEADER + self._make_entries(20), encoding="utf-8"
        )
        logger = ActivityLogger(memory_path=tmp_path, max_lines=20)

        await logger.log_activity(
            prompt="trigger rotation",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )

        archive_content = archive_file.read_text(encoding="utf-8")
        # Should contain the pre-existing entry plus new archived entries
        assert "| old | archived | entry |" in archive_content
        lines = archive_content.strip().splitlines()
        assert len(lines) > 1

    async def test_archive_directory_auto_created(self, tmp_path: Path) -> None:
        activity_file = tmp_path / "activity.md"
        activity_file.write_text(
            ACTIVITY_HEADER + self._make_entries(20), encoding="utf-8"
        )
        logger = ActivityLogger(memory_path=tmp_path, max_lines=20)

        # Archive dir should NOT exist before
        assert not (tmp_path / "archive").exists()

        await logger.log_activity(
            prompt="trigger rotation",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )

        assert (tmp_path / "archive").exists()

    async def test_header_preserved_after_rotation(self, tmp_path: Path) -> None:
        activity_file = tmp_path / "activity.md"
        activity_file.write_text(
            ACTIVITY_HEADER + self._make_entries(20), encoding="utf-8"
        )
        logger = ActivityLogger(memory_path=tmp_path, max_lines=20)

        await logger.log_activity(
            prompt="trigger rotation",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )

        content = activity_file.read_text(encoding="utf-8")
        assert "# Activity Log" in content
        assert "| When | Project | Summary |" in content
        assert "|------|---------|---------|" in content


# ---------------------------------------------------------------------------
# TestActivityConcurrency
# ---------------------------------------------------------------------------


class TestActivityConcurrency:
    """Concurrent log_activity() calls produce correct number of entries."""

    async def test_10_concurrent_calls_produce_10_entries(
        self, logger: ActivityLogger, memory_dir: Path
    ) -> None:
        tasks = [
            logger.log_activity(
                prompt=f"Prompt {i}",
                response_content=f"Response {i}",
                working_directory=Path(f"/home/user/proj-{i}"),
                num_turns=i + 1,
            )
            for i in range(10)
        ]
        await asyncio.gather(*tasks)

        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        data_rows = [
            line
            for line in content.strip().splitlines()
            if line.startswith("|") and "---" not in line and "When" not in line
        ]
        assert len(data_rows) == 10


# ---------------------------------------------------------------------------
# TestActivityFailureSafety
# ---------------------------------------------------------------------------


class TestActivityFailureSafety:
    """log_activity() swallows exceptions and logs structlog warning."""

    async def test_swallows_file_not_found(self, tmp_path: Path) -> None:
        # Point to a non-existent directory
        logger = ActivityLogger(memory_path=tmp_path / "nonexistent", max_lines=200)
        # Should not raise
        result = await logger.log_activity(
            prompt="test",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )
        assert result is None

    async def test_swallows_permission_error(self, tmp_path: Path) -> None:
        activity_file = tmp_path / "activity.md"
        activity_file.write_text(ACTIVITY_HEADER, encoding="utf-8")
        activity_file.chmod(0o000)

        logger = ActivityLogger(memory_path=tmp_path, max_lines=200)
        # Should not raise
        result = await logger.log_activity(
            prompt="test",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )
        assert result is None

        # Restore permissions for cleanup
        activity_file.chmod(0o644)

    async def test_never_raises(self, tmp_path: Path) -> None:
        logger = ActivityLogger(memory_path=tmp_path / "does-not-exist", max_lines=200)
        # Should complete without raising any exception
        try:
            await logger.log_activity(
                prompt="test",
                response_content="resp",
                working_directory=Path("/home/user/proj"),
                num_turns=1,
            )
        except Exception:
            pytest.fail("log_activity() raised an exception")

    async def test_creates_missing_memory_dir(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "fresh-workspace" / "memory"
        logger = ActivityLogger(memory_path=memory_dir, max_lines=200)
        await logger.log_activity(
            prompt="test",
            response_content="resp",
            working_directory=Path("/home/user/proj"),
            num_turns=1,
        )
        content = (memory_dir / "activity.md").read_text(encoding="utf-8")
        assert "[1t]" in content

    async def test_permission_error_disables_logger(self, tmp_path: Path) -> None:
        activity_file = tmp_path / "activity.md"
        activity_file.write_text(ACTIVITY_HEADER, encoding="utf-8")
        activity_file.chmod(0o000)

        logger = ActivityLogger(memory_path=tmp_path, max_lines=200)
        with patch.object(
            logger, "_rotate_if_needed", wraps=logger._rotate_if_needed
        ) as rotate_spy:
            for _ in range(3):
                await logger.log_activity(
                    prompt="test",
                    response_content="resp",
                    working_directory=Path("/home/user/proj"),
                    num_turns=1,
                )
        # First call hits the PermissionError and disables the logger;
        # subsequent calls short-circuit without touching the file again.
        assert logger._disabled is True
        assert rotate_spy.call_count == 1

        # Restore permissions for cleanup
        activity_file.chmod(0o644)
