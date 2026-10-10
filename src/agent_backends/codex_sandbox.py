"""Per-profile sandbox for Codex turns (dv-e38e).

Every Codex turn used to run ``-s danger-full-access``: none of the Claude-side
layers (PreToolUse hook, bwrap ``denyRead``, write boundary) apply to Codex, so
any agent could read ``.env``, the DBs, other users' workspaces and ~/.claude.

codex-cli 0.157 ships named permission profiles (``default_permissions`` +
``[permissions.<name>]``). Verified against the installed 0.157.1 with
``codex sandbox`` (docs/FINDINGS.md 2026-09-27):

- ``-s workspace-write`` and ``-s read-only`` do NOT restrict reads: a command
  can ``cat`` any file the uid can read. Only writes are confined.
- A permission profile's ``filesystem`` table maps absolute paths (globs
  allowed, e.g. ``/x/**/*.db``) or special keys (``:root``,
  ``:project_roots``, ``:slash_tmp``, ``:tmpdir``) to ``read`` / ``write`` /
  ``none``. ``none`` hides the path from reads too (``Permission denied``,
  also for ``grep -r`` from a parent). Nonexistent paths are accepted.
- Commands run in a fresh PID namespace: the bot's ``/proc/<pid>/environ``
  is not visible.
- ``sandbox_mode`` and ``default_permissions`` cannot both be set, so a
  confined turn passes no ``-s``.

The sandbox covers commands the model runs (shell, apply_patch). The codex
process itself (model API, ``$CODEX_HOME`` auth and rollouts, MCP servers
including the agent-tools bridge) runs outside it, so those keep working.

Unrestricted profiles (devops) keep ``danger-full-access``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PERMISSION_PROFILE = "devvy_confined"

# Home-relative credential locations hidden from every confined turn: the
# directory-shaped entries of DEFAULT_DENIED_PATHS plus the account files the
# filename patterns catch on the Claude side.
_HOME_SECRETS = (".ssh", ".aws", ".claude", ".claude.json", ".claude.json.backup")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _legacy_secret_globs(repo_root: Path) -> List[str]:
    """Repo-local secrets and DBs of the legacy (no data root) layout."""
    data = repo_root / "data"
    return [
        str(repo_root / ".env"),
        str(repo_root / ".env.*"),
        str(data / "*.db"),
        str(data / "*.db-*"),
    ]


def other_user_dirs(registry: Any, user_id: Optional[int]) -> List[Path]:
    """Every existing per-user / per-guest workspace not owned by ``user_id``."""
    if registry is None:
        return []
    from src.profiles.registry import GUESTS_DIRNAME, USERS_DIRNAME

    own = str(user_id) if user_id is not None else None
    found: List[Path] = []
    for profile in getattr(registry, "profiles", None) or []:
        for sub in (USERS_DIRNAME, GUESTS_DIRNAME):
            parent = profile.working_dir / sub
            try:
                children = sorted(parent.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name != own and child.is_dir():
                    found.append(child.resolve())
    return found


def denied_paths(
    *,
    full_access: bool,
    registry: Any = None,
    user_id: Optional[int] = None,
    repo_root: Optional[Path] = None,
    home: Optional[Path] = None,
    codex_home: Optional[Path] = None,
) -> List[str]:
    """Paths a confined Codex turn must not read or write, as ``none`` keys."""
    from src.security.denylist import session_protected_dirs
    from src.utils.paths import REPO_ROOT

    home = home or Path.home()
    view = session_protected_dirs(full_access=full_access)
    paths: List[str] = [str(d) for d in view.everyone + view.tenant]
    paths += [str(home / name) for name in _HOME_SECRETS]
    paths.append(str((codex_home or _codex_home()) / "auth.json"))
    paths += _legacy_secret_globs(repo_root or REPO_ROOT)
    paths += [str(d) for d in other_user_dirs(registry, user_id)]
    # Stable order, no duplicates: the argv is logged and diffed in tests.
    return list(dict.fromkeys(paths))


def filesystem_policy(
    *,
    cwd: Path,
    extra_dirs: Iterable[Path] = (),
    full_access: bool,
    denied: Iterable[str],
) -> Dict[str, str]:
    """The ``permissions.<name>.filesystem`` table for one turn.

    Sandboxed profiles read everywhere and write only to the workspace,
    ``extra_dirs`` and temp dirs, like the Claude bwrap jail. A full-access
    profile that is not ``unrestricted`` writes everywhere. Both hide
    ``denied``. More specific paths win over ``:root`` in codex.
    """
    policy: Dict[str, str] = {":root": "write" if full_access else "read"}
    policy[":project_roots"] = "write"
    policy[str(Path(cwd).resolve())] = "write"
    for extra in extra_dirs:
        policy[str(Path(extra).resolve())] = "write"
    policy[":slash_tmp"] = "write"
    policy[":tmpdir"] = "write"
    workspace = Path(cwd).resolve()
    for path in denied:
        # Never hide the turn's own workspace (or an ancestor of it): codex
        # would start in a directory it cannot read.
        if "*" not in path and (
            workspace == Path(path) or Path(path) in workspace.parents
        ):
            continue
        policy[path] = "none"
    return policy


def _toml_inline_table(table: Dict[str, str]) -> str:
    # JSON string escaping is valid TOML basic-string escaping.
    body = ", ".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in table.items())
    return "{" + body + "}"


def sandbox_args(
    *,
    unrestricted: bool,
    full_access: bool,
    cwd: Path,
    extra_dirs: Iterable[Path] = (),
    registry: Any = None,
    user_id: Optional[int] = None,
) -> List[str]:
    """The ``codex exec`` sandbox options for one turn.

    ``unrestricted`` -> ``-s danger-full-access`` (the server-admin profile).
    Anything else -> the confined permission profile, network on (parity with
    today's shell access; the Claude jail allows egress too).
    """
    if unrestricted:
        return ["-s", "danger-full-access"]
    denied = denied_paths(full_access=full_access, registry=registry, user_id=user_id)
    policy = filesystem_policy(
        cwd=cwd, extra_dirs=extra_dirs, full_access=full_access, denied=denied
    )
    name = PERMISSION_PROFILE
    return [
        "-c",
        f'default_permissions="{name}"',
        "-c",
        f"permissions.{name}.filesystem={_toml_inline_table(policy)}",
        "-c",
        f"permissions.{name}.network.enabled=true",
    ]
