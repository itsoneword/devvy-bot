"""The «ready to publish» card and its taps' cards (dv-b05e phase 3).

Pure, like ``card_buttons`` (whose ``button`` and ``clean_buttons`` it shares):
``scripts/release`` (src/release) builds the card it pushes after a release
check PASS (and its daily reminder) with the same code the bot uses on a tap.

Callback data (<= 64 bytes, all owner-gated in the bot, ``publish_taps``):

    publish:go:<board>     → the card turns into a confirmation, runs nothing
    publish:yes:<board>    → scripts/release publish <board> --by owner
    publish:no:<board>     → back to Publish / Later, runs nothing
    publish:later:<board>  → scripts/release later <board> (24 h snooze)
"""

from typing import Any, Mapping, Optional, Sequence, Tuple

from .card_buttons import Rows
from .card_buttons import button as _button

PREFIX = "publish:"


def buttons(board: str) -> Rows:
    """Publish / Later: the card's keyboard, also what a failed publish keeps."""
    return [
        [
            _button("🚀 Опубликовать", f"publish:go:{board}"),
            _button("💤 Позже", f"publish:later:{board}"),
        ]
    ]


def confirm_buttons(board: str) -> Rows:
    return [
        [
            _button("✅ Опубликовать", f"publish:yes:{board}"),
            _button("✖ Отмена", f"publish:no:{board}"),
        ]
    ]


def build(
    board: str,
    target: str,
    rec: Mapping[str, Any],
    checks: Sequence[str],
    skipped: Optional[Mapping[str, str]] = None,
    reminder: str = "",
) -> Tuple[str, Rows]:
    """The card: repo, version, what ships, which checks passed, the changelog
    entry; Publish / Later. ``rec`` is the pump's PASS record (version,
    staged, pending, entry)."""
    head = f"📦 **{board}** v{rec.get('version', '?')} · ready to publish"
    lines = [f"⏰ {reminder}"] if reminder else []
    lines += [head, f"→ {target} (public)"]
    staged = str(rec.get("staged") or "")
    pending = rec.get("pending")
    more = f", {pending} commit(s) vs public" if pending else ""
    lines.append(f"staged {staged[:7]}{more}")
    lines.append("checks passed: " + ", ".join(checks))
    if skipped:
        lines.append(
            "not checked: " + "; ".join(f"{k} — {v}" for k, v in skipped.items())
        )
    lines += ["", str(rec.get("entry") or "(no changelog entry in the report)"), ""]
    lines.append(
        f"Publish: `scripts/release publish {board}` · later: `scripts/release later {board}`"
    )
    return "\n".join(lines), buttons(board)


def confirm_status(board: str) -> str:
    """The line above the card while it waits for the confirming tap."""
    return f"⚠️ Опубликовать {board} публично (push, тег, релиз)? Это не отменить."


def published_status(out: str) -> Optional[str]:
    """``PUBLISHED <sha> <tag> <url>`` (release_public.py publish) → the
    result line; None when the output has no such line."""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "PUBLISHED":
            sha, tag, url = parts[1:4]
            return f"🚀 Опубликовано: {tag} · `{sha[:12]}`\n{url}"
    return None
