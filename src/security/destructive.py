"""Destructive-operation classifier for the confirmation gate.

Used only for **full-access** (unsandboxed) profiles like ``devops``. Sandboxed
profiles can't do cross-FS / network damage from bash, so they are never gated.

The threat model is an *unattended* full-access agent reading a prompt-injection
and silently destroying data. Defence is confirmation, not prohibition: when a
tool call matches one of these patterns the run pauses for a Telegram
approve/deny. The set is a *balanced curated* list — catch the irreversible /
system-level ops, let everyday commands (ls, git status/commit, builds, tests,
edits inside cwd) run freely.

``classify(tool_name, tool_input, working_directory)`` returns a short reason
string when the op should be confirmed, else ``None``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# System roots a write/redirect must never touch without confirmation.
_SYSTEM_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/var",
    "/root",
)

# (compiled pattern, human reason). Order doesn't matter; first match wins.
_BASH_RULES: List[Tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\s+(-\w*\s+)*-\w*[rf]\w*"),
        "recursive/forced file deletion (rm)",
    ),
    (re.compile(r"\brm\s+-[rf]"), "forced file deletion (rm)"),
    (re.compile(r"\bdd\b[^|]*\bof="), "raw disk write (dd)"),
    (re.compile(r"\bmkfs\b"), "filesystem format (mkfs)"),
    (re.compile(r"\bshred\b"), "secure file wipe (shred)"),
    (re.compile(r"\btruncate\b"), "file truncation (truncate)"),
    (re.compile(r"\bwipefs\b"), "filesystem signature wipe (wipefs)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bgit\s+push\b[^\n]*\s(--force|-f)\b"), "force git push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (
        re.compile(r"\bgit\s+clean\b[^\n]*-\w*f"),
        "git clean -f (deletes untracked files)",
    ),
    (re.compile(r"\bchmod\s+-\w*R"), "recursive permission change (chmod -R)"),
    (re.compile(r"\bchown\s+-\w*R"), "recursive ownership change (chown -R)"),
    (
        re.compile(r"\bsystemctl\s+(stop|restart|disable|mask|kill)\b"),
        "service control (systemctl)",
    ),
    (re.compile(r"\b(kill|pkill|killall)\b"), "process termination (kill/pkill)"),
    (
        re.compile(r"\bdocker\b[^\n]*\b(rm|rmi|prune)\b"),
        "docker resource removal (docker rm/prune)",
    ),
    (
        re.compile(r"\b(drop\s+(table|database)|truncate\s+table)\b", re.IGNORECASE),
        "destructive SQL (DROP/TRUNCATE)",
    ),
    (re.compile(r"\bmv\s+[^\n]+\s+/dev/null\b"), "move to /dev/null (data loss)"),
    (
        re.compile(r"\bnode\b(?!\s+--check\b)[^\n]*\b(buy|perp)\.js\b"),
        "real-money exchange order script (buy.js/perp.js -- fetchcrypto or fetchmexc)",
    ),
]

# A redirect ( > or >> ) whose target lands under a system prefix.
_REDIRECT_RE = re.compile(r">>?\s*(?P<path>/[^\s;&|]+)")

# Redirecting a stream *into* these is never destructive: there's no real data
# there to damage (unlike e.g. /dev/sda, an actual device backing store).
_SAFE_REDIRECT_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}

_BASH_TOOLS = {"Bash", "bash", "shell"}
_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "create_file", "edit_file"}


def _under_system_prefix(path_str: str) -> bool:
    try:
        resolved = Path(path_str).expanduser()
    except (ValueError, RuntimeError):
        return False
    p = resolved.as_posix()
    return any(p == pre or p.startswith(pre + "/") for pre in _SYSTEM_PREFIXES)


def _is_safe_redirect_target(path_str: str) -> bool:
    try:
        resolved = Path(path_str).expanduser()
    except (ValueError, RuntimeError):
        return False
    return resolved.as_posix() in _SAFE_REDIRECT_TARGETS


def classify(
    tool_name: str,
    tool_input: Dict[str, Any],
    working_directory: Path,
) -> Optional[str]:
    """Return a reason string if the op needs confirmation, else None."""
    if tool_name in _BASH_TOOLS:
        command = tool_input.get("command", "") or ""
        for pattern, reason in _BASH_RULES:
            if pattern.search(command):
                return reason
        # Redirect into a system path (e.g. `echo x > /etc/hosts`). Discarding
        # a stream into /dev/null (or the equivalent stdout/stderr/stdin
        # pseudo-files) is exempt — there's nothing there to damage.
        for m in _REDIRECT_RE.finditer(command):
            path = m.group("path")
            if _is_safe_redirect_target(path):
                continue
            if _under_system_prefix(path):
                return f"redirect into system path ({path})"
        return None

    if tool_name in _FILE_WRITE_TOOLS:
        path_str = tool_input.get("file_path") or tool_input.get("path")
        if not path_str:
            return None
        # Only system/sensitive locations are gated; ordinary cross-directory
        # writes (e.g. the notes vault) run freely, and the security denylist
        # is the backstop for secret files.
        if _under_system_prefix(str(path_str)):
            return f"write to system path ({path_str})"
        return None

    return None
