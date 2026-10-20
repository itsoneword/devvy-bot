"""The bash box: every Bash command of a sandboxed profile runs inside bwrap.

Claude Code's native sandbox cannot start on this host (Ubuntu's
``bwrap-userns-restrict`` AppArmor profile strips the capabilities its nested
seccomp step needs, FINDINGS 2026-10-17), so it is switched off for sandboxed
profiles and the PreToolUse hook rewrites each Bash command to run in a box
built here (dv-98f2). bwrap is launched by an ordinary process, which this
host allows. The CLI itself, with its login, stays outside the box.

The box is an allowlist, not a read-only view of ``/``:

* read-only: ``/usr``, ``/etc``, ``/opt``, the ``/bin``-style symlinks, the
  user's ``~/.local/{bin,lib}`` and the profile's ``sandbox_read_dirs``;
* read-write: the profile's workspace, its ``extra_dirs`` and the CLI's
  per-session scratch dir (``/tmp/claude-<uid>``, where charts land);
* private: ``/tmp``, ``/run`` (no docker.sock, no user bus), ``$HOME``
  (a tmpfs, so ``~/.claude``, ``data/secrets``, ``data/db``, ``.env``, other
  workspaces and ``~/.ssh`` do not exist), its own PID namespace (the bot's
  ``/proc/<pid>/environ`` is invisible);
* the network is shared: no egress control yet.

Anything not named is absent, so a new secret location is safe by default.
The command is passed to ``bash -c`` as ONE argv element, never re-parsed.
This file doubles as the launcher the rewritten command runs (stdlib only).
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

# System roots mounted read-only. Symlinks (Ubuntu's /bin -> usr/bin) are
# recreated as symlinks; a missing path is skipped.
_SYSTEM_RO = ("/usr", "/etc", "/opt", "/bin", "/sbin", "/lib", "/lib32", "/lib64")
# resolv.conf points into /run/systemd/resolve, and /run is otherwise empty.
_RUN_KEEP = ("/run/systemd/resolve",)
# Per-user tool dirs under $HOME that are safe to expose read-only.
_HOME_RO = (".local/bin", ".local/lib")


class BoxUnavailable(RuntimeError):
    """bwrap is not installed: the caller must fail closed."""


def scratch_dir() -> Path:
    """The CLI's per-session scratch dir (``session_scratch_roots`` agrees)."""
    import tempfile

    return Path(tempfile.gettempdir()) / f"claude-{os.getuid()}"


def _under(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def build_bwrap_argv(
    *,
    write_dirs: Sequence[Path],
    read_dirs: Sequence[Path] = (),
    chdir: Optional[Path] = None,
    home: Optional[Path] = None,
    bwrap_bin: Optional[str] = None,
) -> List[str]:
    """The bwrap argv (up to, not including, ``--`` and the command)."""
    bwrap = bwrap_bin or shutil.which("bwrap")
    if not bwrap:
        raise BoxUnavailable("bwrap is not installed")
    home = home or Path.home()
    scratch = scratch_dir()
    scratch.mkdir(mode=0o700, exist_ok=True)

    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
    ]
    for raw in _SYSTEM_RO:
        p = Path(raw)
        if p.is_symlink():
            argv += ["--symlink", os.readlink(p), raw]
        elif p.exists():
            argv += ["--ro-bind", raw, raw]
    argv += ["--proc", "/proc", "--dev", "/dev"]
    argv += ["--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var/tmp"]
    argv += ["--symlink", "../run", "/var/run"]
    for raw in _RUN_KEEP:
        if Path(raw).exists():
            argv += ["--ro-bind", raw, raw]
    argv += ["--tmpfs", str(home)]
    for rel in _HOME_RO:
        p = home / rel
        if p.is_dir():
            argv += ["--ro-bind", str(p), str(p)]
    # Read-only grants first so a write grant nested inside one wins.
    for d in read_dirs:
        if d.is_dir():
            argv += ["--ro-bind", str(d), str(d)]
    binds = [*write_dirs, scratch]
    for d in binds:
        if d.is_dir():
            argv += ["--bind", str(d), str(d)]
    if chdir is not None and _under(chdir, [d for d in binds if d.is_dir()]):
        argv += ["--chdir", str(chdir)]
    elif write_dirs:
        argv += ["--chdir", str(write_dirs[0])]
    return argv


@dataclass(frozen=True)
class BashBox:
    """One sandboxed profile's box: what it may write and what it may read."""

    write_dirs: tuple[Path, ...]  # workspace first, then extra_dirs
    read_dirs: tuple[Path, ...] = ()

    def wrap(self, command: str, cwd: Optional[str] = None) -> str:
        """*command* rewritten to run inside the box.

        The result runs THIS FILE as a launcher (``main`` below), which builds
        the bwrap argv itself and ``exec``s it. Two reasons it is not a bare
        bwrap line: the CLI's lexical bash checks re-read the rewritten command
        and would trip on bwrap's own ``--dev /dev`` / ``--ro-bind /etc``
        arguments, and the launcher keeps one builder for the box.

        Every word is ``shlex``-quoted and the original command is the single
        argument after ``--``, handed to ``bash -c`` as one argv element, so
        nothing in it is interpreted by an outer shell. Raises
        :class:`BoxUnavailable` when bwrap is missing.
        """
        if not shutil.which("bwrap"):
            raise BoxUnavailable("bwrap is not installed")
        argv = [sys.executable, str(Path(__file__).resolve())]
        if cwd:
            argv += ["--cwd", cwd]
        for d in self.write_dirs:
            argv += ["--rw", str(d)]
        for d in self.read_dirs:
            argv += ["--ro", str(d)]
        return shlex.join([*argv, "--", command])


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Launcher: ``bash_box.py [--cwd D] [--rw D]... [--ro D]... -- COMMAND``."""
    parser = argparse.ArgumentParser(prog="bash_box")
    parser.add_argument("--cwd")
    parser.add_argument("--rw", action="append", default=[])
    parser.add_argument("--ro", action="append", default=[])
    parser.add_argument("command", nargs=1)
    args = parser.parse_args(argv)
    try:
        box = build_bwrap_argv(
            write_dirs=[Path(d) for d in args.rw],
            read_dirs=[Path(d) for d in args.ro],
            chdir=Path(args.cwd) if args.cwd else None,
        )
    except BoxUnavailable as exc:
        print(f"bash box unavailable: {exc}", file=sys.stderr)
        sys.exit(126)
    os.execv(box[0], [*box, "--", "/bin/bash", "-c", args.command[0]])


if __name__ == "__main__":
    main()
