"""One tool-call policy for every backend (dv-1601).

``decide(tool_name, tool_input, turn)`` is the single entry point: the Claude
PreToolUse hook (``src/claude/sdk_integration.py``) and the Codex PreToolUse
hook (``src/agent_bridge/policy_gate.py``, reached over the loopback bridge)
both call it, so a rule added here holds on both by construction.

Three outcomes:

* ``deny``  -- credential files, multi-tenant stores, protected data-root
  areas, guest read scope. Holds for every profile.
* ``ask``   -- destructive or outbound operations on a full-access profile,
  and every non-read-only ``exchange_*`` tool on any profile
  (``src/security/destructive.py``). The caller turns it into an owner
  Approve/Deny button.
* ``allow`` -- everything else.

Where the rule lists live: *deny* lists in ``src/security/denylist.py`` and
``src/claude/monitor.py``; *ask* rules in ``src/security/destructive.py``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .denylist import (
    DEFAULT_DENIED_PATTERNS,
    TENANT_DATA_PATTERNS,
    protected_path_error,
    protected_search_error,
)
from .destructive import _is_free_path, classify, exchange_tool_ask
from .read_scope import ReadScope, build_read_scope

ALLOW = "allow"
DENY = "deny"
ASK = "ask"

_BASH_TOOLS = {"Bash", "bash", "shell"}
# Search tools recurse from their `path` root and print matched lines, so the
# root itself must pass the search-ancestor rule on guest sessions (dv-7de7).
SEARCH_TOOLS = {"Grep", "Glob", "LS"}
_PATH_KEYS = ("file_path", "path", "notebook_path")
# A list of paths (``view_image``'s ``paths``): every entry is a candidate.
_PATH_LIST_KEYS = ("paths",)

# Images are looked at only through ``mcp__agent__view_image`` (dv-275f): a
# raw ``Read`` returns the whole file base64-encoded, which once killed a turn
# (FINDINGS 2026-08-04). By suffix, so the rule stays pure (no stat); SVG is
# XML text and stays readable.
RASTER_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_IMAGE_READ_TOOLS = {"Read"}
_VIEW_IMAGE_TOOL = "mcp__agent__view_image"
# Codex's own image viewer: off in its argv (``features.view_image=false``);
# denied here as the backstop, because it never sees the image roots.
_NATIVE_IMAGE_TOOLS = {"view_image"}

# Codex edits files through `apply_patch`; its input is the patch text.
_PATCH_TOOLS = {"apply_patch"}
_PATCH_FILE = re.compile(
    r"^\*\*\* (?P<op>Add File|Update File|Delete File|Move to): (?P<path>.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TurnPolicy:
    """What the policy needs to know about the turn making the call."""

    working_directory: Path
    # Unsandboxed profile (devops): the only kind the ask-gate applies to.
    full_access: bool = False
    # Profile flag (dv-e38e): skip the data-root areas; credential rules stay.
    unrestricted: bool = False
    read_scope: Optional[ReadScope] = None
    # A pump job's own git worktree (dv-58db): deleting inside it needs no
    # owner. Only ``src/model_router/job.py`` sets it; chat turns never do.
    free_root: Optional[Path] = None


@dataclass(frozen=True)
class Decision:
    action: str = ALLOW
    reason: str = ""
    # What the owner is shown on an ``ask``.
    preview: str = ""


def turn_policy_for(
    registry: Any,
    default_sandbox_enabled: bool,
    working_directory: Path,
    user_id: Optional[int] = None,
    role: Optional[str] = None,
) -> TurnPolicy:
    """Resolve a turn's flags from its profile, the way the SDK path does."""
    from ..bot.utils.image_extractor import session_scratch_roots

    profile = registry.get_by_working_dir(working_directory) if registry else None
    sandbox = getattr(profile, "sandbox", None)
    if sandbox is None:
        sandbox = default_sandbox_enabled
    return TurnPolicy(
        working_directory=Path(working_directory),
        full_access=not sandbox,
        unrestricted=bool(getattr(profile, "unrestricted", False)),
        read_scope=build_read_scope(
            registry=registry,
            working_directory=Path(working_directory),
            user_id=user_id,
            role=role,
            scratch_roots=session_scratch_roots(),
        ),
    )


def is_secret_path(file_path: str) -> bool:
    """True when *file_path*'s basename matches a credential pattern."""
    name = Path(file_path).name
    return any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_DENIED_PATTERNS)


def is_tenant_data(file_path: str) -> bool:
    """True when *file_path* is one of the shared multi-tenant SQLite stores."""
    name = Path(file_path).name
    return any(fnmatch.fnmatch(name, pat) for pat in TENANT_DATA_PATTERNS)


def glob_literal_prefix(pattern: str) -> str:
    """Directory part of a glob pattern before its first metacharacter."""
    cut = len(pattern)
    for ch in ("*", "?", "[", "{"):
        idx = pattern.find(ch)
        if idx != -1:
            cut = min(cut, idx)
    literal = pattern[:cut]
    if cut < len(pattern):
        # `secrets/a*` -> `secrets`: drop the partial name being globbed.
        literal = literal.rsplit("/", 1)[0] if "/" in literal else ""
    return literal


def _patch_operations(patch: str, cwd: Path) -> List[Tuple[str, str]]:
    """``(operation, absolute path)`` for every file an apply_patch touches."""
    out: List[Tuple[str, str]] = []
    for match in _PATCH_FILE.finditer(patch):
        path = Path(match.group("path")).expanduser()
        if not path.is_absolute():
            path = cwd / path
        out.append((match.group("op"), str(path)))
    return out


def _deny_reason(
    tool_name: str, tool_input: Dict[str, Any], turn: TurnPolicy
) -> Optional[str]:
    """The denylist: rules that hold for every profile and every tool call."""
    cwd = turn.working_directory
    if tool_name in _BASH_TOOLS:
        command = tool_input.get("command", "")
        if command:
            # Lazy: src.claude imports this module at load time.
            from ..claude.monitor import check_bash_denied_paths

            ok, error = check_bash_denied_paths(
                command,
                cwd,
                full_access=turn.full_access,
                read_scope=turn.read_scope,
                unrestricted=turn.unrestricted,
            )
            if not ok:
                return error or "Denied path"
        return None

    if tool_name in _NATIVE_IMAGE_TOOLS:
        return (
            f"Codex's built-in view_image is switched off: use {_VIEW_IMAGE_TOOL} "
            f"(same arguments: path), which downscales and checks the path."
        )
    file_path = tool_input.get("file_path")
    if (
        tool_name in _IMAGE_READ_TOOLS
        and isinstance(file_path, str)
        and Path(file_path).suffix.lower() in RASTER_IMAGE_SUFFIXES
    ):
        return (
            f"Reading image files is blocked: a raw image can kill the turn and "
            f"wastes context. Use the view_image tool ({_VIEW_IMAGE_TOOL}) -- "
            f"it downscales the image and checks the path."
        )

    candidates: List[str] = [
        value
        for value in (tool_input.get(key) for key in _PATH_KEYS)
        if isinstance(value, str) and value
    ]
    for key in _PATH_LIST_KEYS:
        values = tool_input.get(key)
        if isinstance(values, list):
            candidates.extend(v for v in values if isinstance(v, str) and v)
    # Glob's `pattern` may itself be a path (`/root/secrets/**`); its
    # literal prefix is checked like a search root (dv-2c1c).
    pattern = tool_input.get("pattern")
    if tool_name == "Glob" and isinstance(pattern, str):
        prefix = glob_literal_prefix(pattern)
        if prefix and ("/" in prefix or prefix.startswith("~")):
            base = tool_input.get("path") or str(cwd)
            candidates.append(str(Path(base) / Path(prefix).expanduser()))

    for candidate in candidates:
        # Data-root areas (dv-2c1c): secrets/ + archive/ for everyone,
        # db/ for sandboxed profiles. Directory-shaped, so checked by
        # resolved prefix, not by basename like the patterns below.
        area_error = protected_path_error(
            candidate, cwd, turn.full_access, unrestricted=turn.unrestricted
        )
        if area_error is None and tool_name in SEARCH_TOOLS:
            area_error = protected_search_error(
                candidate, cwd, turn.full_access, unrestricted=turn.unrestricted
            )
        if area_error is not None:
            return area_error
        if is_secret_path(candidate):
            return (
                f"Access denied: '{candidate}' is a credential file. "
                f"Secrets are not readable from an agent session by any "
                f"profile -- ask the owner to read or change it directly."
            )
        if not turn.full_access and is_tenant_data(candidate):
            return (
                f"Access denied: '{candidate}' holds every user's private "
                f"data. Use the memory and tools this session already "
                f"provides for the current user."
            )
        # Guest read scope (dv-7de7): cross-user trees + allowlist. Both
        # checks no-op on owner sessions.
        if turn.read_scope is not None:
            ok, error = turn.read_scope.check_read(candidate)
            if not ok:
                return error or "Denied path"
            if tool_name in SEARCH_TOOLS:
                ok, error = turn.read_scope.check_search_root(candidate)
                if not ok:
                    return error or "Denied search root"
    return None


def _decide_one(
    tool_name: str, tool_input: Dict[str, Any], turn: TurnPolicy
) -> Decision:
    reason = _deny_reason(tool_name, tool_input, turn)
    if reason:
        return Decision(DENY, reason)
    # Exchange tools ask on every profile: a money move is not something a
    # sandbox makes safe (dv-8664).
    exchange = exchange_tool_ask(tool_name, tool_input)
    if exchange:
        return Decision(ASK, exchange[0], exchange[1])
    # Ask-gate: full-access profiles only; sandboxed profiles can't do
    # cross-FS/network damage from bash.
    if turn.full_access:
        reason = classify(tool_name, tool_input, turn.working_directory, turn.free_root)
        if reason:
            preview = (
                tool_input.get("command")
                or tool_input.get("file_path")
                or tool_input.get("path")
                or ""
            )
            return Decision(ASK, reason, str(preview))
    return Decision()


def decide(tool_name: str, tool_input: Dict[str, Any], turn: TurnPolicy) -> Decision:
    """Allow, deny or ask for one tool call. Pure: no I/O beyond path resolution."""
    tool_name = tool_name or ""
    tool_input = tool_input or {}
    command = tool_input.get("command")

    if tool_name in _PATCH_TOOLS and isinstance(command, str):
        # One patch can touch several files: every one must pass as a write.
        ask: Optional[Decision] = None
        for operation, path in _patch_operations(command, turn.working_directory):
            decision = _decide_one("Write", {"file_path": path}, turn)
            if (
                decision.action == ALLOW
                and operation == "Delete File"
                and turn.full_access
                and not _is_free_path(path, turn.working_directory, turn.free_root)
            ):
                decision = Decision(ASK, "file deletion (apply_patch)", path)
            if decision.action == DENY:
                return decision
            if decision.action == ASK and ask is None:
                ask = decision
        return ask or Decision()

    if (
        tool_name not in _BASH_TOOLS
        and isinstance(command, str)
        and command
        and not any(key in tool_input for key in _PATH_KEYS)
        and not tool_name.startswith("mcp__")
    ):
        # A shell-shaped tool under a name we don't know (a new harness
        # version, another backend): judge it as a shell command.
        tool_name = "Bash"

    return _decide_one(tool_name, tool_input, turn)
