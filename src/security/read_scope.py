"""Per-session read scope: cross-user tree denial + guest read allowlist.

dv-7de7. Two facts composed badly: cross-agent reads are allowed by design
(dv-6dbc), and denylist mode never compares reads against the working
directory — so the read surface was "everything on the box minus a filename
pattern list", including other users' private ``workspaces/*/users/<id>`` and
``workspaces/*/guests/<id>`` trees.

Posture (owner decision 2026-08-08, amended at sign-off):

- **Owners keep unrestricted cross-agent reads.** The cross-user tree denial
  and the search-ancestor rule activate only for guest sessions
  (``guest_allowlist is not None``). Home secrets directories (``~/.ssh/``,
  ``~/.aws/``, ``~/.claude/``) are denied to every role, but that lives in
  ``src/security/denylist.py``, not here.
- **Guests get a read allowlist**: their own per-user dirs, the working dirs
  of every guest-reachable profile (derived from ``allowed_roles``), and the
  session scratch roots. Everything else — including other users' trees and
  anything personal under ``$HOME`` — is denied.
- **Search-ancestor rule** (guests only): a ``Grep``/``Glob``/``LS`` or bash
  recursive read whose root is an *ancestor* of a denied tree is refused,
  because the traversal would print matched lines from files the path checks
  never see individually.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from src.profiles.registry import ProfileRegistry

# Subdirectories under ~/.claude/ that Claude Code uses internally and must
# stay readable AND writable from any session (plan mode writes plan files).
_CLAUDE_INTERNAL_SUBDIRS: Set[str] = {"plans", "todos", "settings.json"}

# Additionally readable (but NOT write-exempt): plugin/skill code the CLI may
# legitimately execute or load. Kept separate so the write-containment checks
# never treat them as fair game — a guest writing into ~/.claude/plugins/
# would be code injection into every future session.
_CLAUDE_READONLY_SUBDIRS: Set[str] = {"plugins", "skills"}


def _is_claude_internal_path(
    file_path: str, *, include_readonly_dirs: bool = False
) -> bool:
    """Check whether *file_path* points inside ``~/.claude/`` (allowed subdirs only).

    ``include_readonly_dirs`` additionally accepts the read-only plugin/skill
    dirs; use it for READ exemptions only, never for write containment.
    """
    try:
        resolved = Path(file_path).resolve()
        home = Path.home().resolve()
        claude_dir = home / ".claude"

        # Path must be inside ~/.claude/
        try:
            rel = resolved.relative_to(claude_dir)
        except ValueError:
            return False

        # Must be in one of the known subdirectories (or a known file)
        top_part = rel.parts[0] if rel.parts else ""
        allowed = _CLAUDE_INTERNAL_SUBDIRS
        if include_readonly_dirs:
            allowed = allowed | _CLAUDE_READONLY_SUBDIRS
        return top_part in allowed

    except Exception:
        return False


def home_denied_dirs() -> Tuple[Path, ...]:
    """Home directories denied to EVERY role (mirrors the denylist prefixes)."""
    home = Path.home().resolve()
    return (home / ".ssh", home / ".aws", home / ".claude")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class ReadScope:
    """Per-session read policy, built once per ``execute_command``.

    ``guest_allowlist is None`` marks an owner session: every check in here
    becomes a no-op and today's unrestricted cross-agent reads are preserved.
    """

    working_directory: Path
    own_roots: Tuple[Path, ...]
    denied_user_trees: Tuple[Path, ...]
    guest_allowlist: Optional[Tuple[Path, ...]]

    @property
    def is_guest(self) -> bool:
        return self.guest_allowlist is not None

    def _resolve(self, path: str | Path) -> Optional[Path]:
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self.working_directory / target
            return target.expanduser().resolve()
        except (ValueError, OSError, RuntimeError):
            return None

    def _in_own_roots(self, resolved: Path) -> bool:
        return any(_is_within(resolved, root) for root in self.own_roots)

    def _denied_tree_for(self, resolved: Path) -> Optional[Path]:
        for tree in self.denied_user_trees:
            if _is_within(resolved, tree):
                return tree
        return None

    def check_read(self, path: str | Path) -> Tuple[bool, Optional[str]]:
        """Gate a single-file/tool read. No-op for owner sessions."""
        if self.guest_allowlist is None:
            return True, None
        resolved = self._resolve(path)
        if resolved is None:
            return False, f"Could not resolve path: {path}"
        if _is_claude_internal_path(str(resolved)):
            return True, None
        if self._in_own_roots(resolved):
            return True, None
        tree = self._denied_tree_for(resolved)
        if tree is not None:
            return False, (
                f"Access denied: '{path}' is inside another user's private "
                f"workspace tree ({tree}). Per-user directories under "
                f"workspaces/*/users/ and workspaces/*/guests/ are never "
                f"readable from a guest session."
            )
        if not any(_is_within(resolved, root) for root in self.guest_allowlist):
            return False, (
                f"Access denied: '{path}' is outside this guest session's "
                f"readable scope. Guest sessions may read their own workspace, "
                f"the shared guest-reachable profile workspaces, and the "
                f"session scratch dir."
            )
        return True, None

    def check_search_root(self, path: str | Path) -> Tuple[bool, Optional[str]]:
        """Gate a recursive search root (Grep/Glob/LS, bash grep/find/...).

        Denies roots that are ancestors of a denied user tree or of a denied
        home secrets dir: the traversal would surface contents of files the
        per-path checks never see. Guests only — owners are unrestricted.
        """
        if self.guest_allowlist is None:
            return True, None
        resolved = self._resolve(path)
        if resolved is None:
            return False, f"Could not resolve search root: {path}"
        if self._in_own_roots(resolved):
            return True, None
        # Own dirs are exempt individually (check_read), but a denied tree
        # itself still shields OTHER users' subdirs, so no exemption applies
        # at tree level.
        targets: List[Path] = list(self.denied_user_trees)
        targets.extend(home_denied_dirs())
        for target in targets:
            if resolved == target or _is_within(target, resolved):
                return False, (
                    f"Access denied: searching '{path}' would traverse a "
                    f"protected directory ({target}). Search a narrower "
                    f"directory or your own workspace instead."
                )
        return True, None

    def check_bash_read(self, resolved: Path) -> Tuple[bool, Optional[str]]:
        """Gate a resolved bash path token. No-op for owner sessions.

        Deliberately narrower than ``check_read``: a strict allowlist would
        deny ``/usr/bin/python3``, ``/etc/hosts`` and every library path, so
        bash confinement applies only (a) to the cross-user trees, and (b) to
        anything under ``$HOME`` — which is where everything personal lives.
        Non-home system paths stay governed by the plain denylist.
        """
        if self.guest_allowlist is None:
            return True, None
        if _is_claude_internal_path(str(resolved)):
            return True, None
        if self._in_own_roots(resolved):
            return True, None
        tree = self._denied_tree_for(resolved)
        if tree is not None:
            return False, (
                f"Access denied: '{resolved}' is inside another user's "
                f"private workspace tree ({tree}). Per-user directories are "
                f"never readable from a guest session."
            )
        home = Path.home().resolve()
        if _is_within(resolved, home) and not any(
            _is_within(resolved, root) for root in self.guest_allowlist
        ):
            return False, (
                f"Access denied: '{resolved}' is outside this guest "
                f"session's readable scope. Guest sessions may only touch "
                f"their own workspace and the shared guest-reachable profile "
                f"workspaces."
            )
        return True, None


def build_read_scope(
    registry: Optional["ProfileRegistry"],
    working_directory: Path,
    user_id: Optional[int] = None,
    role: Optional[str] = None,
    scratch_roots: Optional[List[Path]] = None,
) -> ReadScope:
    """Build the per-session :class:`ReadScope`.

    Guest detection: explicit ``role == "guest"``, or a working directory of
    the shape ``<profile root>/guests/<uid>`` (covers webhook/scheduler
    callers that pass ``role=None`` but run in a guest cwd). An owner on a
    ``personal_memory`` profile runs in ``<root>/users/<uid>`` — the parent
    dir name is what distinguishes the two, NOT the presence of a user id
    (``resolve_working_dir`` returns an id for both shapes).
    """
    from src.profiles.registry import (
        GUESTS_DIRNAME,
        guest_working_dir,
        user_working_dir,
    )

    wd = working_directory.expanduser().resolve()
    own_roots: List[Path] = [wd]
    for scratch in scratch_roots or []:
        try:
            own_roots.append(Path(scratch).resolve())
        except (ValueError, OSError):
            continue

    denied_user_trees: List[Path] = []
    guest_reachable_roots: List[Path] = []
    is_guest = role == "guest"
    effective_user_id = user_id

    if registry is not None:
        profile, ws_user_id = registry.resolve_working_dir(wd)
        if effective_user_id is None:
            effective_user_id = ws_user_id
        if not is_guest and wd.parent.name == GUESTS_DIRNAME and profile is not None:
            # role=None caller in a guest-shaped cwd: fail closed to guest.
            is_guest = True
        for p in registry.profiles:
            root = p.working_dir.resolve()
            denied_user_trees.append(root / "users")
            denied_user_trees.append(root / "guests")
            if "guest" in p.allowed_roles:
                guest_reachable_roots.append(root)
                if effective_user_id is not None:
                    # Both shapes, so a future role change never strands files.
                    own_roots.append(guest_working_dir(p, effective_user_id).resolve())
                    own_roots.append(user_working_dir(p, effective_user_id).resolve())

    if not is_guest:
        return ReadScope(
            working_directory=wd,
            own_roots=tuple(own_roots),
            denied_user_trees=tuple(denied_user_trees),
            guest_allowlist=None,
        )

    allowlist: List[Path] = list(own_roots) + guest_reachable_roots
    return ReadScope(
        working_directory=wd,
        own_roots=tuple(own_roots),
        denied_user_trees=tuple(denied_user_trees),
        guest_allowlist=tuple(allowlist),
    )
