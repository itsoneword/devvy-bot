from pathlib import Path

from src.bot.orchestration.routing import apply_route
from src.profiles.registry import ProfileDefinition, ProfileRegistry


def _p(slug, root, default=False):
    return ProfileDefinition(
        slug, slug, Path(root), Path(root) / "m", Path(root) / "p.md", default=default
    )


def _reg():
    return ProfileRegistry(
        [_p("devops", "/tmp/dev", default=True), _p("channel", "/tmp/chan")]
    )


def test_apply_route_sticky_switch_changes_active_and_dir():
    user_data = {"active_profile": "devops", "current_directory": Path("/tmp/dev")}
    decision = apply_route("/channel read", user_data, _reg())
    assert user_data["active_profile"] == "channel"
    assert decision.working_dir == Path("/tmp/chan")
    assert decision.prompt == "read"
    assert decision.badge.startswith("channel") or "channel" in decision.badge
    assert decision.restore_dir is None


def test_apply_route_one_shot_keeps_active_and_restores():
    user_data = {"active_profile": "devops", "current_directory": Path("/tmp/dev")}
    decision = apply_route("@channel hi", user_data, _reg())
    assert user_data["active_profile"] == "devops"
    assert decision.working_dir == Path("/tmp/chan")
    assert decision.restore_dir == Path("/tmp/dev")


def test_apply_route_plain_uses_active():
    user_data = {"active_profile": "devops", "current_directory": Path("/tmp/dev")}
    decision = apply_route("hello", user_data, _reg())
    assert decision.working_dir == Path("/tmp/dev")
    assert decision.prompt == "hello"
