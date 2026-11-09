"""Workspace boundary used by both layers of detached job confinement."""

from pathlib import Path


def cwd_in_profile(cwd: Path, profile_root: Path) -> bool:
    resolved = cwd.resolve()
    root = profile_root.resolve()
    return resolved == root or (
        resolved.parent == root / "users" and resolved.name.isdigit()
    )
