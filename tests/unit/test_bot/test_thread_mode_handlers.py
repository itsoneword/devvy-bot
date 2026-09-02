"""Tests for /sync_threads thread-mode constraints."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.commands import system
from src.config import create_test_config


@pytest.fixture
def thread_settings(tmp_path: Path):
    approved = tmp_path / "projects"
    approved.mkdir()
    project_root = approved / "project_a"
    project_root.mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )

    settings = create_test_config(
        approved_directory=str(approved),
        enable_project_threads=True,
        project_threads_mode="private",
        projects_config_path=str(config_file),
    )
    return settings, project_root


async def test_sync_threads_private_mode_rejects_non_private_chat(thread_settings):
    """sync_threads in private mode must run in private chat."""
    settings, _ = thread_settings

    manager = AsyncMock()
    manager.sync_topics = AsyncMock()

    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "group"
    update.effective_chat.id = -1001
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
        "project_threads_manager": manager,
    }
    context.user_data = {}

    await system.sync_threads(update, context)

    manager.sync_topics.assert_not_called()
    status_msg.edit_text.assert_called_once()


async def test_sync_threads_reloads_registry_from_yaml(thread_settings, monkeypatch):
    """sync_threads should reload YAML registry at runtime before syncing."""
    settings, _ = thread_settings

    manager = AsyncMock()
    manager.sync_topics = AsyncMock(
        return_value=MagicMock(
            created=0,
            reused=2,
            renamed=0,
            reopened=0,
            closed=0,
            deactivated=0,
            failed=0,
        )
    )

    new_registry = MagicMock()
    load_mock = MagicMock(return_value=new_registry)
    monkeypatch.setattr(system, "load_project_registry", load_mock)

    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "private"
    update.effective_chat.id = 42
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
        "project_threads_manager": manager,
        "project_registry": MagicMock(),
    }
    context.user_data = {}

    await system.sync_threads(update, context)

    load_mock.assert_called_once_with(
        config_path=settings.projects_config_path,
        approved_directory=settings.approved_directory,
    )
    assert manager.registry is new_registry
    assert context.bot_data["project_registry"] is new_registry
    manager.sync_topics.assert_called_once()


async def test_sync_threads_group_mode_rejects_non_target_chat(tmp_path: Path):
    """sync_threads in group mode must be called from configured target chat."""
    approved = tmp_path / "projects"
    approved.mkdir()
    project_root = approved / "project_a"
    project_root.mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )

    settings = create_test_config(
        approved_directory=str(approved),
        enable_project_threads=True,
        project_threads_mode="group",
        project_threads_chat_id=-10012345,
        projects_config_path=str(config_file),
    )

    manager = AsyncMock()
    manager.sync_topics = AsyncMock()

    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = -10099999
    update.message.reply_text = AsyncMock(return_value=status_msg)

    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "audit_logger": None,
        "project_threads_manager": manager,
    }
    context.user_data = {}

    await system.sync_threads(update, context)

    manager.sync_topics.assert_not_called()
    status_msg.edit_text.assert_called_once()
