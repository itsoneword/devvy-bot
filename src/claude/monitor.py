"""Bash directory boundary enforcement for Claude tool calls."""

import fnmatch
import re
import shlex
from pathlib import Path
from typing import List, Optional, Set, Tuple

# Moved to src/security/read_scope.py (dv-7de7) so the denylist can apply the
# ~/.claude/ exemption without importing this module; re-exported here for
# compatibility (sdk_integration and tests import them from monitor).
from src.security.read_scope import (  # noqa: F401
    _CLAUDE_INTERNAL_SUBDIRS,
    ReadScope,
    _is_claude_internal_path,
)

# Commands that modify the filesystem or change context and should have paths checked
_FS_MODIFYING_COMMANDS: Set[str] = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "install",
    "tee",
    "cd",
}

# Commands that are read-only or don't take filesystem paths
_READ_ONLY_COMMANDS: Set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
    "which",
    "whoami",
    "pwd",
    "echo",
    "printf",
    "env",
    "printenv",
    "date",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
    "dirname",
    "basename",
}

# Actions / expressions that make ``find`` a filesystem-modifying command
_FIND_MUTATING_ACTIONS: Set[str] = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}

# Bash command separators
_COMMAND_SEPARATORS: Set[str] = {"&&", "||", ";", "|", "&"}


def check_bash_directory_boundary(
    command: str,
    working_directory: Path,
    approved_directory: Optional[Path],
) -> Tuple[bool, Optional[str]]:
    """Check if a bash command's paths stay within allowed boundaries.

    If approved_directory is set, uses allowlist mode (paths must be inside it).
    If approved_directory is None, uses denylist mode (paths must not be in
    denied locations).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True, None

    if not tokens:
        return True, None

    command_chains: list[list[str]] = []
    current_chain: list[str] = []

    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            if current_chain:
                command_chains.append(current_chain)
            current_chain = []
        else:
            current_chain.append(token)

    if current_chain:
        command_chains.append(current_chain)

    resolved_approved = approved_directory.resolve() if approved_directory else None

    # Import denylist lazily only when needed
    denylist = None
    if resolved_approved is None:
        from src.security.denylist import DenylistValidator

        denylist = DenylistValidator()

    for cmd_tokens in command_chains:
        if not cmd_tokens:
            continue

        base_command = Path(cmd_tokens[0]).name

        if base_command in _READ_ONLY_COMMANDS:
            continue

        needs_check = False
        if base_command == "find":
            needs_check = any(t in _FIND_MUTATING_ACTIONS for t in cmd_tokens[1:])
        elif base_command in _FS_MODIFYING_COMMANDS:
            needs_check = True

        if not needs_check:
            continue

        for token in cmd_tokens[1:]:
            if token.startswith("-"):
                continue

            try:
                if token.startswith("/"):
                    resolved = Path(token).resolve()
                else:
                    resolved = (working_directory / token).resolve()

                if denylist:
                    # Denylist mode
                    allowed, error = denylist.is_path_allowed(str(resolved))
                    if not allowed:
                        return False, (
                            f"Directory boundary violation: '{base_command}' "
                            f"targets '{token}' which is a denied location"
                        )
                elif resolved_approved is not None:
                    # Allowlist mode
                    if not _is_within_directory(resolved, resolved_approved):
                        return False, (
                            f"Directory boundary violation: "
                            f"'{base_command}' targets "
                            f"'{token}' which is outside approved directory "
                            f"'{resolved_approved}'"
                        )
            except (ValueError, OSError):
                continue

    return True, None


# Sensitive targets recognised even when they are *inside* a quoted argument
# rather than a standalone token -- e.g.
#   python3 -c "import sqlite3; sqlite3.connect('data/bot.db')"
# where shlex yields the whole script as one token and path resolution can
# never see it. Token resolution further down still runs (it catches denied
# *paths* like /etc/shadow that have no distinctive basename). (dv-6dbc)
#
# Literals with essentially no false-positive risk: they do not occur inside
# ordinary identifiers or English words, so a raw substring match is safe and is
# the hardest to evade.
_SECRET_SUBSTRINGS: Set[str] = {
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".htpasswd",
    ".pgpass",
    ".netrc",
}

# Denied only for sandboxed profiles -- see TENANT_DATA_PATTERNS in
# src/security/denylist.py for why devops keeps access.
_TENANT_DATA_SUBSTRINGS: Set[str] = {
    "bot.db",
    "llm_analytics.db",
    "channel_intel.db",
    # dv-5f3a: the per-user data store; sidecars (-wal/-shm) contain it as a
    # substring so they are covered too.
    "data.db",
}

# Ambiguous ones. `.env` is a substring of `os.environ`, and `credentials` is an
# ordinary English word -- matching those raw denied `python3 -c "...
# os.environ[...]"` and `grep -rn credentials src/`, which devops runs
# constantly. These are matched against extracted path-like candidates instead
# (basename vs the denylist patterns), which still catches a path embedded in a
# quoted script but ignores it inside an identifier.
_AMBIGUOUS_SECRET_PATTERNS: Set[str] = {
    ".env",
    ".env.*",
    "*.token",
    "auth.json",
    "*credentials*.json",
}

# Quoted regions are scanned separately from whitespace tokens so that a path
# inside `python3 -c "...connect('data/bot.db')..."` is still seen.
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")

# Commands that recurse into a directory tree and PRINT file contents (or pack
# them up). A denied file under an allowed root is invisible to per-path
# checks, so their directory arguments go through the search-ancestor rule
# (ReadScope.check_search_root) on guest sessions. (dv-7de7)
_RECURSIVE_READ_COMMANDS: Set[str] = {"grep", "rg", "ag", "ack", "find", "tar"}

# Shell glob metacharacters: tokens are seen pre-expansion, so the literal
# prefix up to the first metacharacter is checked instead. Best-effort.
_GLOB_CHARS = ("*", "?", "[")


def _strip_glob(token: str) -> str:
    """Literal prefix of *token* up to the first glob metacharacter."""
    cut = len(token)
    for ch in _GLOB_CHARS:
        idx = token.find(ch)
        if idx != -1:
            cut = min(cut, idx)
    return token[:cut]


def _path_candidates(command: str) -> Set[str]:
    """Basenames of every path-like fragment in *command*."""
    fragments: List[str] = []
    for match in _QUOTED.finditer(command):
        fragments.append(match.group(1) or match.group(2) or "")
    fragments.extend(command.split())

    names: Set[str] = set()
    for fragment in fragments:
        cleaned = fragment.strip("'\"()[],;:")
        if not cleaned:
            continue
        names.add(cleaned.rsplit("/", 1)[-1])
    return names


def check_bash_denied_paths(
    command: str,
    working_directory: Path,
    full_access: bool = False,
    read_scope: Optional[ReadScope] = None,
) -> Tuple[bool, Optional[str]]:
    """Reject bash commands that touch denied paths, read or write.

    ``check_bash_directory_boundary`` only inspects *filesystem-modifying*
    commands, so it lets ``cat``/``sqlite3``/``python3`` read anything. That is
    the hole dv-6dbc documents: a guest-tier session read another user's memory
    facts and messages straight out of ``data/bot.db``. Reads of ordinary files
    outside the workspace stay allowed -- cross-agent reads are permitted by
    design; only the denylist is enforced here.

    ``full_access`` (devops) still cannot read secrets, but keeps access to the
    multi-tenant stores: it is the admin profile and DB queries are its job.

    ``read_scope`` (dv-7de7, default ``None`` = prior behavior) adds, on GUEST
    sessions only: cross-user tree denial, home confinement of read targets,
    and the search-ancestor rule for recursive readers (grep/rg/find/...).
    Owner sessions carry ``guest_allowlist=None`` and every scope check
    no-ops.
    """
    lowered = command.lower()

    for needle in _SECRET_SUBSTRINGS:
        if needle in lowered:
            return False, (
                f"Access denied: command references '{needle}', a credential "
                f"file. Secrets are not readable from an agent session by any "
                f"profile -- ask the owner to read or change it directly."
            )

    candidates = _path_candidates(command)
    for name in candidates:
        for pattern in _AMBIGUOUS_SECRET_PATTERNS:
            if fnmatch.fnmatch(name, pattern):
                return False, (
                    f"Access denied: command references '{name}', a credential "
                    f"file. Secrets are not readable from an agent session by "
                    f"any profile -- ask the owner to read or change it "
                    f"directly."
                )

    if not full_access:
        for needle in _TENANT_DATA_SUBSTRINGS:
            if needle in lowered:
                return False, (
                    f"Access denied: command references '{needle}', a store "
                    f"holding every user's private data (memory facts, "
                    f"messages, tokens). Use the memory and tools this session "
                    f"already provides for the current user."
                )

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes -- the substring pass above already ran, which is
        # the part that matters; don't guess at the rest.
        return True, None

    from src.security.denylist import DenylistValidator

    denylist = DenylistValidator()

    def _resolve_token(token: str) -> Optional[Path]:
        try:
            if token.startswith("/"):
                return Path(token).resolve()
            if token.startswith("~"):
                return Path(token).expanduser().resolve()
            # Only treat it as a path if it looks like one; bare words are
            # command names and flags, not files.
            if "/" not in token and not token.startswith("."):
                return None
            return (working_directory / token).resolve()
        except (ValueError, OSError):
            return None

    for token in tokens:
        if token.startswith("-") or token in _COMMAND_SEPARATORS:
            continue
        resolved = _resolve_token(_strip_glob(token) if read_scope else token)
        if resolved is None:
            continue

        allowed, error = denylist.is_path_allowed(str(resolved))
        if not allowed:
            return False, (f"Access denied: '{token}' is a denied location ({error})")

        if read_scope is not None:
            allowed, error = read_scope.check_bash_read(resolved)
            if not allowed:
                return False, error

    # Recursive readers (grep/rg/find/tar/...): their directory arguments get
    # the search-ancestor rule, because the traversal prints matched lines
    # from files the per-path checks above never see. Guest sessions only --
    # check_search_root no-ops for owners. (dv-7de7)
    if read_scope is not None and read_scope.is_guest:
        chains: List[List[str]] = []
        current: List[str] = []
        for token in tokens:
            if token in _COMMAND_SEPARATORS:
                if current:
                    chains.append(current)
                current = []
            else:
                current.append(token)
        if current:
            chains.append(current)

        for cmd_tokens in chains:
            if not cmd_tokens:
                continue
            if Path(cmd_tokens[0]).name not in _RECURSIVE_READ_COMMANDS:
                continue
            for token in cmd_tokens[1:]:
                if token.startswith("-"):
                    continue
                resolved_dir = _resolve_token(_strip_glob(token))
                if resolved_dir is None:
                    continue
                allowed, error = read_scope.check_search_root(resolved_dir)
                if not allowed:
                    return False, error

    return True, None


_REDIRECT_OPS: Set[str] = {">", ">>", "1>", "2>", "&>", ">|", "2>>", "&>>"}


def check_bash_workspace_writes(
    command: str,
    working_directory: Path,
    extra_roots: Optional[list[Path]] = None,
) -> Tuple[bool, Optional[str]]:
    """Confine bash *writes* to the workspace on sandboxed profiles.

    The CLI sandbox already makes the filesystem read-only outside the
    workspace, so this is mostly defence in depth -- with one case where it is
    the only control: ``sandbox_excluded_commands`` (git, npm, pip, poetry,
    make) are deliberately run OUTSIDE the bash sandbox, so nothing else stops
    ``git init ~/elsewhere`` (outside the workspace) from a guest-reachable
    profile.

    Checks shell redirect targets and the path arguments of filesystem-modifying
    commands. Reads are untouched -- cross-agent reads are allowed by design.
    """
    roots = [working_directory.resolve(), *(extra_roots or [])]

    def _contained(path_str: str) -> bool:
        try:
            if path_str.startswith("~"):
                resolved = Path(path_str).expanduser().resolve()
            elif path_str.startswith("/"):
                resolved = Path(path_str).resolve()
            else:
                resolved = (working_directory / path_str).resolve()
        except (ValueError, OSError):
            return True  # unparseable: leave it to the other checks
        if _is_claude_internal_path(str(resolved)):
            return True
        return any(_is_within_directory(resolved, root) for root in roots)

    try:
        tokens = shlex.split(command)
    except ValueError:
        return True, None

    targets: list[str] = []

    for idx, token in enumerate(tokens):
        if token in _REDIRECT_OPS:
            if idx + 1 < len(tokens):
                targets.append(tokens[idx + 1])
            continue
        # Attached form: `>file`, `>>file`, `2>file`
        for op in (">>", ">"):
            marker = token.find(op)
            if marker != -1 and token[marker + len(op) :]:
                prefix = token[:marker]
                if prefix in ("", "1", "2", "&"):
                    targets.append(token[marker + len(op) :])
                break

    # Path arguments of filesystem-modifying commands.
    current: list[str] = []
    chains: list[list[str]] = []
    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            if current:
                chains.append(current)
            current = []
        else:
            current.append(token)
    if current:
        chains.append(current)

    for cmd_tokens in chains:
        if not cmd_tokens:
            continue
        base = Path(cmd_tokens[0]).name
        if base not in _FS_MODIFYING_COMMANDS:
            continue
        for token in cmd_tokens[1:]:
            if token.startswith("-") or token in _REDIRECT_OPS:
                continue
            targets.append(token)

    for target in targets:
        if not _contained(target):
            return False, (
                f"Access denied: '{target}' is outside this agent's workspace "
                f"({working_directory}). Reads outside are fine; writes must "
                f"stay in the workspace or the session scratch dir."
            )

    return True, None


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Check if path is within directory."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False
