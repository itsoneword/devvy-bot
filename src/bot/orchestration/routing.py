"""Profile routing: access control and route application for user messages.

Extracted verbatim from src/bot/orchestrator.py (behavior-preserving move).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ...profiles.registry import ProfileRegistry, ensure_workspace_for
from ...profiles.routing import resolve_route

_PROFILE_BADGES = {
    "devops": "🛠 devops",
    "channel": "📣 channel",
    "general": "🧰 general",
}


class ProfileAccessDenied(Exception):
    """A role tried to route into a profile its permission policy disallows."""

    def __init__(self, slug: str, role: str):
        self.slug = slug
        self.role = role
        super().__init__(f"role '{role}' may not access profile '{slug}'")


@dataclass
class RouteDecision:
    working_dir: Path
    prompt: str
    badge: str
    restore_dir: Optional[Path]  # for one-shot: dir to restore after the call


_PERSISTED_PREF_KEYS = ("active_profile", "agent_backend", "agent_model")


def seed_prefs_into_user_data(user_data: dict, prefs: Optional[Dict[str, Any]]) -> None:
    """Copy saved prefs into user_data for any key not already set.

    None values are ignored. Existing user_data values win (a choice made
    earlier in the same process must not be overwritten by the DB row).
    """
    if not prefs:
        return
    for key in _PERSISTED_PREF_KEYS:
        value = prefs.get(key)
        if value is not None and not user_data.get(key):
            user_data[key] = value


def apply_route(
    text: str,
    user_data: dict,
    registry: ProfileRegistry,
    role: str = "owner",
    user_id: Optional[int] = None,
) -> RouteDecision:
    """Mutate user_data's active profile/dir per the route, return the decision.

    ``role`` gates profile access: guests may only route into profiles whose
    permission policy allows them. A guest's default/active profile falls back
    to the guest default (sandboxed ``general``), never the registry default.

    For guests (``role != "owner"`` with a ``user_id``) the effective working
    dir is the per-guest workspace ``<profile.working_dir>/guests/<user_id>``,
    created on first use. Owners keep the profile root. Because SDK transcripts
    and bot windows both key off working_dir, this isolates files, auto-resume,
    and token windows per guest.

    Raises ProfileAccessDenied when the message targets a disallowed profile
    (sticky or one-shot), or when no profile allows the role at all.
    """
    active = user_data.get("active_profile") or registry.get_default().slug
    if role != "owner":
        # Guard the persisted active profile: if it is missing or no longer
        # guest-allowed, fall back to the role's default profile.
        active_profile = registry.get_by_slug(active)
        if active_profile is None or not active_profile.allows_role(role):
            fallback = registry.get_default_for_role(role)
            if fallback is None:
                raise ProfileAccessDenied(active, role)
            active = fallback.slug
            user_data["active_profile"] = active
    valid = {p.slug for p in registry.profiles}
    r = resolve_route(text, active=active, valid_slugs=valid)

    target = registry.get_by_slug(r.target) or registry.get_default()
    if not target.allows_role(role):
        raise ProfileAccessDenied(target.slug, role)
    badge = _PROFILE_BADGES.get(target.slug, target.slug)
    prior_dir = user_data.get("current_directory")

    # Per-user workspace when the role or the profile demands one. Guests get
    # one because they are untrusted; owners get one on personal_memory
    # profiles because the topic is private per person (see needs_own_workspace).
    working_dir = ensure_workspace_for(target, role, user_id)

    if r.kind == "one_shot":
        return RouteDecision(
            working_dir=working_dir,
            prompt=r.prompt,
            badge=badge,
            restore_dir=Path(str(prior_dir)) if prior_dir else None,
        )

    if r.new_active:
        user_data["active_profile"] = r.new_active
    user_data["current_directory"] = working_dir
    return RouteDecision(
        working_dir=working_dir, prompt=r.prompt, badge=badge, restore_dir=None
    )
