"""UI around a profile the boot switched off (dv-86f4 phase 2)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.orchestration.agents_menu import build_agents_manage
from src.bot.orchestrator import MessageOrchestrator
from src.profiles.registry import ProfileDefinition, ProfileRegistry

REASON = "Profile 'gymbro': extra_dirs entry is not a directory: /x/gymbro-staging"


def _reg(tmp_path: Path) -> ProfileRegistry:
    def p(slug: str, default: bool = False) -> ProfileDefinition:
        root = tmp_path / slug
        return ProfileDefinition(
            slug, slug.title(), root, root / "m", root / "p.md", default
        )

    return ProfileRegistry([p("devops", True), p("gymbro")])


def test_boot_disabled_profile_is_not_a_pending_toggle(tmp_path):
    # Real shape: the YAML still says enabled, the live registry says off.
    file_reg, live_reg = _reg(tmp_path), _reg(tmp_path)
    live_reg.disable_for_run("gymbro", REASON)
    text, markup = build_agents_manage(file_reg, live_reg)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🔄 Restart to apply" not in labels
    assert any(
        t.startswith("✅ Gymbro (gymbro)") and "off this run" in t for t in labels
    )
    assert "/x/gymbro-staging" in text


async def test_stale_switch_button_says_why_profile_is_off(tmp_path):
    reg = _reg(tmp_path)
    reg.disable_for_run("gymbro", REASON)
    orch = MessageOrchestrator(
        SimpleNamespace(agentic_mode=True, agent_backend="claude", agent_model="x"),
        {},
    )
    context = SimpleNamespace(
        user_data={"role": "owner"},
        bot_data={"storage": None, "profile_registry": reg},
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1), edit_message_text=AsyncMock()
    )
    await orch._handle_agent_callback(query, context, "agent:gymbro")
    assert "active_profile" not in context.user_data
    args, _ = query.edit_message_text.call_args
    assert "off this run" in args[0] and "/x/gymbro-staging" in args[0]
