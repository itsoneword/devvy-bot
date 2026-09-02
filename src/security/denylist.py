"""Denylist-based path validation.

Instead of restricting access to one directory (allowlist), this blocks
specific sensitive paths while allowing everything else. Used when the
bot runs as a native service with full user-level access.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Optional, Tuple

import structlog

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
        self.denied_paths = (
            denied_paths if denied_paths is not None else list(DEFAULT_DENIED_PATHS)
        )
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

    def is_path_allowed(self, file_path: str) -> Tuple[bool, Optional[str]]:
        """Check if a path is allowed (not in the denylist).

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
        for denied in self.denied_paths:
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
