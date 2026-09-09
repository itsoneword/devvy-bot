"""/agents picker + Manage view rendering (dv-ffb4)."""

from pathlib import Path

from src.bot.orchestration.agents_menu import build_agents_manage, build_agents_picker
from src.profiles.registry import ProfileDefinition, ProfileRegistry


def _def(slug: str, **kw) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug.title(),
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        **kw,
    )


REG = ProfileRegistry(
    [
        _def("devops", default=True, sandbox=False),
        _def("general", allowed_roles=frozenset({"guest"})),
        _def("pm", enabled=False),
    ]
)


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def test_picker_owner_sees_enabled_profiles_and_manage_row():
    text, markup = build_agents_picker(REG, active="general", role="owner")
    assert "general" in text
    data = [d for _, d in _buttons(markup)]
    assert data == ["agent:devops", "agent:general", "agents:manage", "agents:new"]
    assert any(t.startswith("✅ General") for t, _ in _buttons(markup))


def test_picker_guest_sees_only_reachable_profiles_no_manage():
    _, markup = build_agents_picker(REG, active="general", role="guest")
    assert [d for _, d in _buttons(markup)] == ["agent:general"]


def test_manage_lists_all_with_state_and_no_toggle_on_default():
    _, markup = build_agents_manage(REG, REG)
    buttons = _buttons(markup)
    assert buttons[0] == ("✅ Devops (devops) — default", "agents:manage")
    assert ("✅ General (general)", "agents:toggle:general") in buttons
    assert ("⛔ Pm (pm)", "agents:toggle:pm") in buttons
    assert "agents:restart" not in [d for _, d in buttons]
    assert buttons[-1][1] == "agents:back"


def test_manage_offers_restart_when_file_differs_from_live():
    file_reg = ProfileRegistry(
        [_def("devops", default=True, sandbox=False), _def("general"), _def("pm")]
    )
    text, markup = build_agents_manage(file_reg, REG)
    data = [d for _, d in _buttons(markup)]
    assert "agents:restart" in data
    assert "restart" in text
    assert ("✅ Pm (pm)", "agents:toggle:pm") in _buttons(markup)
