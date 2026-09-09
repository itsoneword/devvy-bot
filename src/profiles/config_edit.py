"""Text-level edits to ``config/profiles.yaml`` that keep its comments.

The registry file is hand-annotated per-machine config (see
``config/profiles.example.yaml``); a YAML round-trip through ``yaml.safe_dump``
would strip every comment. So the one edit the bot makes at runtime — the
``enabled:`` flag flipped from the /agents Manage menu (dv-ffb4) — is a
line-level splice, the same approach ``scripts/install_profiles.py`` takes
for appending entries.

The edited text is validated with ``load_profile_registry`` *before* it is
written, so a bad splice can never produce a file that aborts the next boot.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .registry import load_profile_registry

_ENTRY_RE = re.compile(r"""^(\s*)-\s*slug:\s*["']?([A-Za-z0-9_-]+)["']?\s*(#.*)?$""")
_ENABLED_RE = re.compile(r"^(\s*)enabled:\s*\S+(\s*#.*)?$")
_KEY_RE = re.compile(r"^(\s*)[A-Za-z_][A-Za-z0-9_]*:")


def _entry_bounds(lines: list[str], slug: str) -> tuple[int, int]:
    """Line range ``[start, end)`` of the ``- slug: <slug>`` entry."""
    start = -1
    for i, line in enumerate(lines):
        m = _ENTRY_RE.match(line)
        if m and m.group(2) == slug:
            start = i
            break
    if start < 0:
        raise ValueError(f"Profile '{slug}' not found in profiles config")
    entry_indent = len(_ENTRY_RE.match(lines[start]).group(1))  # type: ignore[union-attr]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        # Next list item at the same level, or a shallower key (`profiles:`
        # sibling), closes the entry.
        if indent <= entry_indent and (_ENTRY_RE.match(line) or _KEY_RE.match(line)):
            end = j
            break
    return start, end


def render_profile_enabled(text: str, slug: str, enabled: bool) -> str:
    """Return ``text`` with ``enabled: <value>`` set on the ``slug`` entry.

    Pure: no I/O, no validation. An existing ``enabled:`` line is rewritten
    in place (its trailing comment kept); otherwise one is inserted right
    after the ``- slug:`` line at the entry's key indent. ``enabled: true``
    is the default, so enabling *removes* the key rather than writing a
    redundant one — the file stays as the owner wrote it.
    """
    lines = text.splitlines(keepends=True)
    start, end = _entry_bounds(lines, slug)
    value = "true" if enabled else "false"
    for i in range(start + 1, end):
        m = _ENABLED_RE.match(lines[i].rstrip("\n"))
        if m:
            if enabled:
                del lines[i]
            else:
                comment = m.group(2) or ""
                lines[i] = f"{m.group(1)}enabled: {value}{comment}\n"
            return "".join(lines)
    if enabled:
        return text  # absent == enabled; nothing to write
    # Indent of the entry's other keys, if any; else "- " + 2 past the dash.
    key_indent = None
    for i in range(start + 1, end):
        m = _KEY_RE.match(lines[i])
        if m and not lines[i].lstrip().startswith("-"):
            key_indent = m.group(1)
            break
    if key_indent is None:
        dash = _ENTRY_RE.match(lines[start]).group(1)  # type: ignore[union-attr]
        key_indent = dash + "  "
    lines.insert(start + 1, f"{key_indent}enabled: {value}\n")
    return "".join(lines)


def set_profile_enabled(
    config_path: Path, slug: str, enabled: bool, *, sandbox_default: bool = True
) -> None:
    """Flip ``enabled:`` for one profile on disk, validated, atomically.

    Raises ``ValueError`` (from the registry loader) without touching the
    file when the result would not load — e.g. disabling the default
    profile. Takes effect on the next boot; the caller offers the restart.
    """
    original = config_path.read_text(encoding="utf-8")
    updated = render_profile_enabled(original, slug, enabled)
    if updated == original:
        return
    fd, tmp_name = tempfile.mkstemp(
        prefix=".profiles.", suffix=".yaml", dir=str(config_path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(updated)
        # Validate the candidate in place: relative paths and ${DEVVY_ROOT}
        # resolve against the config's directory, and tmp shares it.
        load_profile_registry(tmp, sandbox_default=sandbox_default)
        os.replace(tmp, config_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
