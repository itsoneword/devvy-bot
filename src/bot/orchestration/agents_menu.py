"""/agents keyboards: the picker and the owner-only Manage view (dv-ffb4).

Pure rendering — no Telegram I/O — so both the command and the callbacks
draw the same screens and the tests can assert on them directly.

Callback data emitted here (all dispatched in ``callbacks.py``):

    agent:<slug>          switch (pre-existing)
    agents:manage         open the Manage view
    agents:toggle:<slug>  flip enabled for one profile
    agents:back           return to the picker
    agents:restart        apply pending toggles (SIGTERM, systemd restarts)
    agents:new            switch to devops and post the creation intake
"""

from __future__ import annotations

from typing import List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...profiles.registry import ProfileRegistry
from ..utils.html_format import escape_html

# The profile that runs the creation interview (dv-0e1e). It is the
# registry default on every install, but named explicitly so the Manage
# view keeps working if someone makes another profile the default.
CREATOR_SLUG = "devops"

NEW_AGENT_INTAKE = (
    "🛠 <b>New agent</b> — you're now talking to <b>{creator}</b>.\n\n"
    "Describe the agent in one message starting with <code>new agent:</code> — "
    "what it is for, who will use it, and which folders outside its own "
    "workspace it needs (a notes vault, another project). Devops will ask "
    "the rest, write the profile, and offer a restart."
)


def build_agents_picker(
    registry: ProfileRegistry, *, active: str, role: str
) -> Tuple[str, InlineKeyboardMarkup]:
    """One button per enabled profile the role may reach; owner gets Manage."""
    rows: List[List[InlineKeyboardButton]] = []
    for p in registry.enabled_profiles:
        if not p.allows_role(role):
            continue
        mark = "✅ " if p.slug == active else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{p.name} ({p.slug})", callback_data=f"agent:{p.slug}"
                )
            ]
        )
    if role == "owner":
        rows.append(
            [
                InlineKeyboardButton("⚙ Manage", callback_data="agents:manage"),
                InlineKeyboardButton("➕ New agent", callback_data="agents:new"),
            ]
        )
    text = f"<b>Active agent:</b> <code>{escape_html(active)}</code>"
    return text, InlineKeyboardMarkup(rows)


def build_agents_manage(
    file_registry: ProfileRegistry, live_registry: ProfileRegistry
) -> Tuple[str, InlineKeyboardMarkup]:
    """Every profile with its on/off state; tap flips it in the YAML.

    Rendered from ``file_registry`` (a fresh parse of profiles.yaml) rather
    than the running one, so a toggle shows immediately. Where the two
    disagree a restart row is offered — the running registry is boot-only.
    The default profile is listed but has no toggle: the loader refuses to
    disable it.
    """
    live = {p.slug: p.enabled for p in live_registry.profiles}
    pending = any(
        live.get(p.slug, p.enabled) != p.enabled for p in file_registry.profiles
    )
    rows: List[List[InlineKeyboardButton]] = []
    for p in file_registry.profiles:
        state = "✅" if p.enabled else "⛔"
        if p.default:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{state} {p.name} ({p.slug}) — default",
                        callback_data="agents:manage",
                    )
                ]
            )
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    f"{state} {p.name} ({p.slug})",
                    callback_data=f"agents:toggle:{p.slug}",
                )
            ]
        )
    if pending:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Restart to apply", callback_data="agents:restart"
                )
            ]
        )
    rows.append([InlineKeyboardButton("↩ Back", callback_data="agents:back")])
    lines = ["<b>Manage agents</b> — tap to enable/disable."]
    if pending:
        lines.append("Saved; takes effect after a restart.")
    else:
        lines.append("Disabled agents keep their memory and workspace.")
    return "\n".join(lines), InlineKeyboardMarkup(rows)
