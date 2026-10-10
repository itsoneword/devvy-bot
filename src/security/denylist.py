"""Denylist-based path validation.

Instead of restricting access to one directory (allowlist), this blocks
specific sensitive paths while allowing everything else. Used when the
bot runs as a native service with full user-level access.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

import structlog

if TYPE_CHECKING:
    from src.utils.paths import DataLayout

logger = structlog.get_logger()

DEFAULT_DENIED_PATHS: List[str] = [
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/",
    "/boot/",
    "/root/",
    "/proc/kcore",
    "/sys/",
    "/dev/",
    # --- Home secrets directories (dv-7de7) ---------------------------------
    # Denied as PREFIXES for every role: the pattern list below only matches
    # specific filenames, so e.g. ~/.ssh/authorized_keys, ~/.ssh/config and
    # any key not named id_* were readable. The prefix matcher compares
    # against the resolved absolute path, so these must be absolute and are
    # computed at import time. ~/.claude/ carries an exemption for Claude
    # Code's own runtime dirs -- see is_path_allowed.
    str(Path.home() / ".ssh") + "/",
    str(Path.home() / ".aws") + "/",
    str(Path.home() / ".claude") + "/",
]

DEFAULT_DENIED_PATTERNS: List[str] = [
    "*.pem",
    "*.key",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_dsa",
    "id_dsa.*",
    # --- Secrets ------------------------------------------------------------
    # Denied for EVERY profile including devops (dv-6dbc acceptance criteria):
    # a token read is unrecoverable, and no agent task needs the raw file.
    ".env",
    ".env.*",
    "*.token",
    "auth.json",
    "*credentials*.json",
    ".htpasswd*",
    ".pgpass",
    ".netrc",
    # Shell history and Claude Code account state (dv-7de7): both verified
    # readable from a guest-tier profile and neither is directory-shaped, so
    # they live here rather than in DEFAULT_DENIED_PATHS.
    ".bash_history",
    ".zsh_history",
    ".claude.json",
    ".claude.json.backup",
]

# Multi-tenant data stores (dv-6dbc). Every profile runs as the same Unix user,
# so file modes cannot separate tenants: any session that opens these reads
# every OTHER user's memory facts, messages, sessions and tokens. Verified live
# 2026-08-02 from a guest-tier session.
#
# Kept SEPARATE from DEFAULT_DENIED_PATTERNS because the two have different
# audiences. Secrets above are denied to everyone including devops -- a leaked
# token is unrecoverable. These are denied only to sandboxed (guest-reachable)
# profiles: devops is the admin profile and answering "how many messages today"
# is legitimate admin work, so blocking it would be the "new friction" R6
# forbids. The bot's own access is in-process (aiosqlite), never through agent
# tools, so neither list costs the bot anything.
TENANT_DATA_PATTERNS: List[str] = [
    "bot.db",
    "bot.db-*",
    "llm_analytics.db",
    "llm_analytics.db-*",
    "channel_intel.db",
    "channel_intel.db-*",
    # dv-5f3a: the per-user data store (logs/products/portions/issues/docs).
    # Denied BEFORE real data lands in it; agents go through the MCP lanes.
    "data.db",
    "data.db-*",
]


class DenylistValidator:
    """Validates paths against a denylist of sensitive locations."""

    def __init__(
        self,
        denied_paths: Optional[List[str]] = None,
        denied_patterns: Optional[List[str]] = None,
    ) -> None:
        # Default list only. The data root's everyone-denied dirs (secrets,
        # archive; dv-2c1c) are checked per call in ``is_path_allowed``, from
        # ``session_protected_dirs``, so an ``unrestricted`` session (dv-e38e)
        # can skip them while keeping every rule above.
        self._protected_areas = denied_paths is None
        if denied_paths is None:
            denied_paths = list(DEFAULT_DENIED_PATHS)
        self.denied_paths = denied_paths
        self.denied_patterns = (
            denied_patterns
            if denied_patterns is not None
            else list(DEFAULT_DENIED_PATTERNS)
        )
        logger.info(
            "Denylist validator initialized",
            denied_paths_count=len(self.denied_paths),
            denied_patterns_count=len(self.denied_patterns),
        )

    def is_path_allowed(
        self, file_path: str, unrestricted: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Check if a path is allowed (not in the denylist).

        ``unrestricted`` (the devops admin profile, dv-e38e) skips the data
        root's protected dirs only; the baseline prefixes and secret filename
        patterns still apply.

        Returns:
            Tuple of (is_allowed, error_message).
        """
        if not file_path or not file_path.strip():
            return False, "Empty path not allowed"

        file_path = file_path.strip()

        # ~/.claude/ is a denied prefix, but Claude Code legitimately uses a
        # few subdirs at runtime (~/.claude/plans, todos, settings.json) plus
        # the read-only plugin/skill dirs. Exempt those FIRST, or adding the
        # prefix breaks plan mode on every profile. Lazy import: read_scope
        # imports the profile registry and this module must stay light.
        from src.security.read_scope import _is_claude_internal_path

        if _is_claude_internal_path(file_path, include_readonly_dirs=True):
            return True, None

        # Check against denied directory prefixes
        denied_paths = list(self.denied_paths)
        if self._protected_areas:
            denied_paths += [
                str(d) + "/"
                for d in session_protected_dirs(
                    full_access=True, unrestricted=unrestricted
                ).everyone
            ]
        for denied in denied_paths:
            if denied.endswith("/"):
                # Directory prefix match
                if file_path.startswith(denied) or file_path == denied.rstrip("/"):
                    logger.warning("Denylist blocked path", path=file_path, rule=denied)
                    return (
                        False,
                        f"Access denied: path matches denied location '{denied}'",
                    )
            else:
                # Exact file match
                if file_path == denied:
                    logger.warning("Denylist blocked path", path=file_path, rule=denied)
                    return (
                        False,
                        f"Access denied: path matches denied file '{denied}'",
                    )

        # Check filename against denied patterns
        filename = Path(file_path).name
        for pattern in self.denied_patterns:
            if fnmatch.fnmatch(filename, pattern):
                logger.warning(
                    "Denylist blocked pattern", path=file_path, pattern=pattern
                )
                return (
                    False,
                    f"Access denied: filename matches denied pattern '{pattern}'",
                )

        return True, None


# --- Data-root areas (dv-2c1c, epic dv-fcab) --------------------------------
# Directory-shaped denials derived from the storage layout (src/utils/paths.py),
# so they follow DEVVY_DATA_ROOT instead of a hardcoded path:
#
# - ``everyone``: key material (``<root>/secrets``, legacy ``data/model_auth``)
#   and the archive bin. Denied to EVERY profile including devops, like the
#   secret filename patterns above.
# - ``tenant``: ``<root>/db``, denied to sandboxed profiles only, for the same
#   reason as TENANT_DATA_PATTERNS (devops keeps admin DB access). Empty in
#   the legacy layout, where the DB basenames are matched instead.
#
# Agents never need either: user data reaches them through in-process MCP
# tools (src/claude/user_data_tools.py, aiosqlite in the bot process), and
# the Codex token is read by the bot, not by the agent.


@dataclass(frozen=True)
class ProtectedDirs:
    """Resolved data-root areas no agent tool may touch."""

    everyone: Tuple[Path, ...]
    tenant: Tuple[Path, ...]

    def for_session(
        self, full_access: bool, unrestricted: bool = False
    ) -> Tuple[Path, ...]:
        view = self.session_view(full_access, unrestricted)
        return view.everyone + view.tenant

    def session_view(
        self, full_access: bool, unrestricted: bool = False
    ) -> "ProtectedDirs":
        """THE per-session deny set; every layer derives from this.

        - ``unrestricted`` (profile flag, dv-e38e; devops): nothing. The owner
          runs devops as the server admin, so secrets/, archive/ and db/ stay
          reachable. Baseline rules outside the data root (``.env``/key
          filename patterns, ~/.ssh, ~/.claude, ...) still apply.
        - ``full_access`` (sandbox off): secrets/ + archive/.
        - sandboxed: secrets/ + archive/ + db/.
        """
        if unrestricted:
            return ProtectedDirs(everyone=(), tenant=())
        if full_access:
            return ProtectedDirs(everyone=self.everyone, tenant=())
        return self


def _resolve_under_repo(path: Path) -> Path:
    from src.utils.paths import REPO_ROOT

    if not path.is_absolute():
        # Legacy paths are relative to the bot's cwd, which is the checkout.
        path = REPO_ROOT / path
    return path.resolve()


def build_protected_dirs(
    layout: Optional["DataLayout"] = None,
    archive: Optional[Path] = None,
) -> ProtectedDirs:
    """Protected dirs for ``layout`` (default: the configured layout)."""
    from src.utils.paths import archive_dir, data_layout

    if layout is None:
        layout = data_layout()
    if archive is None:
        archive = archive_dir(layout)
    everyone = [_resolve_under_repo(p) for p in layout.secret_dirs]
    everyone.append(_resolve_under_repo(archive))
    tenant = []
    if layout.tenant_db_dir is not None:
        tenant.append(_resolve_under_repo(layout.tenant_db_dir))
    return ProtectedDirs(everyone=tuple(everyone), tenant=tuple(tenant))


@lru_cache(maxsize=1)
def protected_dirs() -> ProtectedDirs:
    """The configured layout's protected dirs, computed once per process.

    DEVVY_DATA_ROOT only changes with a restart, so caching is safe; tests
    call ``protected_dirs.cache_clear()`` after patching the environment.
    """
    return build_protected_dirs()


def session_protected_dirs(
    full_access: bool,
    unrestricted: bool = False,
    dirs: Optional[ProtectedDirs] = None,
) -> ProtectedDirs:
    """The configured layout's deny set for one session (see ``session_view``)."""
    dirs = protected_dirs() if dirs is None else dirs
    return dirs.session_view(full_access, unrestricted)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_agent_path(path: str | Path, working_directory: Path) -> Optional[Path]:
    """Resolve a tool/bash path the way the agent's process would see it."""
    try:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path(working_directory) / target
        return target.resolve()
    except (ValueError, OSError, RuntimeError):
        return None


def protected_path_error(
    path: str | Path,
    working_directory: Path,
    full_access: bool,
    dirs: Optional[ProtectedDirs] = None,
    unrestricted: bool = False,
) -> Optional[str]:
    """Error message when ``path`` is inside a protected dir, else None."""
    dirs = session_protected_dirs(full_access, unrestricted, dirs)
    resolved = resolve_agent_path(path, working_directory)
    if resolved is None:
        return None
    for root in dirs.everyone:
        if _within(resolved, root):
            logger.warning("Denylist blocked protected dir", path=str(resolved))
            return (
                f"Access denied: '{path}' is inside {root}, which holds keys "
                f"or archived files. No agent session may read or change it "
                f"-- ask the owner directly."
            )
    for root in dirs.tenant:
        if _within(resolved, root):
            logger.warning("Denylist blocked tenant dir", path=str(resolved))
            return (
                f"Access denied: '{path}' is inside {root}, the databases "
                f"holding every user's private data. Use the memory and "
                f"data tools this session already provides."
            )
    return None


def protected_search_error(
    root: str | Path,
    working_directory: Path,
    full_access: bool,
    dirs: Optional[ProtectedDirs] = None,
    unrestricted: bool = False,
) -> Optional[str]:
    """Error when a recursive search/copy rooted at ``root`` would traverse a
    protected dir (Grep prints matched lines from files no per-path check
    sees). A protected dir inside the session's own working directory is
    exempt: that is legacy devops (cwd = the checkout, ``data/model_auth``
    inside it), where denying every search from cwd would break the profile.
    """
    view = session_protected_dirs(full_access, unrestricted, dirs)
    resolved = resolve_agent_path(root, working_directory)
    if resolved is None:
        return None
    wd = resolve_agent_path(working_directory, working_directory) or resolved
    for target in view.everyone + view.tenant:
        if _within(target, resolved) and not _within(target, wd):
            return (
                f"Access denied: searching '{root}' would traverse {target}, "
                f"which no agent session may read. Search a narrower directory."
            )
    return None


def sandbox_deny_read(
    full_access: bool,
    dirs: Optional[ProtectedDirs] = None,
    unrestricted: bool = False,
) -> List[str]:
    """``sandbox.filesystem.denyRead`` for the Claude CLI's bwrap jail.

    The CLI mounts an empty tmpfs over each entry, so bash inside the jail
    cannot see those dirs at all. Only existing dirs are listed: a mount point
    that does not exist is not something to gamble a sandbox start on.
    """
    view = session_protected_dirs(full_access, unrestricted, dirs)
    return [str(d) for d in view.everyone + view.tenant if d.is_dir()]
