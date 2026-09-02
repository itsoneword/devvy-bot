"""ActivityLogger: appends structured activity entries to activity.md.

Handles file rotation when exceeding max_lines and protects concurrent
writes with asyncio.Lock. All failures are swallowed with a structlog
warning -- activity logging never blocks user-facing responses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()


class ActivityLogger:
    """Append structured activity log entries to activity.md with rotation."""

    def __init__(
        self,
        memory_path: Path,
        max_lines: int = 200,
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        self._activity_path = memory_path / "activity.md"
        self._archive_dir = memory_path / "archive"
        self._max_lines = max_lines
        self._lock = write_lock or asyncio.Lock()
        self._disabled = False

    async def log_activity(
        self,
        prompt: str,
        response_content: str,
        working_directory: Path,
        num_turns: int,
    ) -> None:
        """Append an activity entry. Never raises -- swallows all exceptions.

        A PermissionError disables the logger for the process lifetime (with a
        single warning) instead of re-warning on every turn: the path stays
        unwritable until an operator fixes ownership/mode and restarts.
        """
        if self._disabled:
            return None
        try:
            async with self._lock:
                self._rotate_if_needed()
                self._append_entry(
                    prompt, response_content, working_directory, num_turns
                )
        except PermissionError:
            self._disabled = True
            logger.warning(
                "activity_log_disabled_permission_denied",
                activity_path=str(self._activity_path),
                hint="fix file/dir ownership for the service user and restart",
                exc_info=True,
            )
        except Exception:
            logger.warning(
                "activity_log_failed",
                activity_path=str(self._activity_path),
                exc_info=True,
            )
        return None

    def _format_entry(
        self,
        prompt: str,
        response_content: str,
        working_directory: Path,
        num_turns: int,
    ) -> str:
        """Format a single activity log entry as a markdown table row."""
        now = datetime.now(UTC)
        timestamp = now.strftime("%Y-%m-%d %H:%M")
        project = working_directory.name
        input_snippet = self._truncate(prompt)
        output_snippet = self._truncate(response_content)
        return f"| {timestamp} | {project} | [{num_turns}t] {input_snippet} -> {output_snippet} |"

    @staticmethod
    def _truncate(text: str, max_len: int = 100) -> str:
        """Collapse whitespace/newlines, truncate to max_len with '...' suffix."""
        clean = " ".join(text.split())
        if len(clean) <= max_len:
            return clean
        return clean[: max_len - 3] + "..."

    def _rotate_if_needed(self) -> None:
        """Move older entries to archive if activity.md exceeds max_lines."""
        if not self._activity_path.exists():
            return

        content = self._activity_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        if len(lines) <= self._max_lines:
            return

        # Identify header: everything up to and including the |---| separator
        header_lines: list[str] = []
        separator_idx = -1
        for i, line in enumerate(lines):
            header_lines.append(line)
            if line.startswith("|---") or line.startswith("| ---"):
                separator_idx = i
                break

        if separator_idx == -1:
            # No separator found -- can't safely rotate
            return

        # Data rows are lines starting with | after the separator
        data_rows: list[str] = []
        non_data_after_header: list[str] = []
        for line in lines[separator_idx + 1 :]:
            if line.startswith("|"):
                data_rows.append(line)
            else:
                non_data_after_header.append(line)

        # Keep roughly half of max_lines (minus header) worth of data rows
        header_count = len(header_lines)
        keep_count = max((self._max_lines - header_count) // 2, 1)

        if len(data_rows) <= keep_count:
            # Not enough data rows to warrant rotation
            return
        archived_rows = data_rows[:-keep_count]
        kept_rows = data_rows[-keep_count:]

        # Write archived rows to archive file
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self._archive_dir / f"activity-{today}.md"
        with open(archive_path, "a", encoding="utf-8") as f:
            for row in archived_rows:
                f.write(row + "\n")

        # Rewrite activity.md with header + kept rows
        new_content_lines = header_lines + kept_rows
        self._activity_path.write_text(
            "\n".join(new_content_lines) + "\n", encoding="utf-8"
        )

    def _append_entry(
        self,
        prompt: str,
        response_content: str,
        working_directory: Path,
        num_turns: int,
    ) -> None:
        """Append a formatted entry to activity.md, creating the dir if needed."""
        entry = self._format_entry(
            prompt, response_content, working_directory, num_turns
        )
        self._activity_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._activity_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
