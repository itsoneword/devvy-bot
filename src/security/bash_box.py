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

Keys (dv-0277): a profile's ``sandbox_keys`` names key-store services. The
launcher, which runs OUTSIDE the box, reads them from the store, puts them in
the boxed command's environment and redacts every value from its output (the
``scripts/withkeys`` contract; stdout and stderr are merged). Inside the box
the store does not exist, so a boxed command cannot ask for another service.
Redaction stops an accidental print, not a determined agent (``base64``,
network): a key handed to a box must carry its own spend limit.

Codex (dv-28f2): Codex has its own OS sandbox, and bwrap cannot nest inside
it, so a Codex turn gets the key VALUES in the codex child's env from
:func:`load_key_env` and the policy gate rewrites each Bash call with
:meth:`BashBox.wrap_env` into this launcher's ``--redact-env NAME`` mode: no
bwrap, no store read, the named vars' values scrubbed from merged output.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

# System roots mounted read-only. Symlinks (Ubuntu's /bin -> usr/bin) are
# recreated as symlinks; a missing path is skipped.
_SYSTEM_RO = ("/usr", "/etc", "/opt", "/bin", "/sbin", "/lib", "/lib32", "/lib64")
# resolv.conf points into /run/systemd/resolve, and /run is otherwise empty.
_RUN_KEEP = ("/run/systemd/resolve",)
# Per-user tool dirs under $HOME that are safe to expose read-only.
_HOME_RO = (".local/bin", ".local/lib")
# Shortest value worth redacting (keystore._MIN_REDACT; this file stays
# stdlib-only, so the constant is repeated, not imported).
_MIN_REDACT = 6


class BoxUnavailable(RuntimeError):
    """bwrap is not installed: the caller must fail closed."""


class KeyEnvError(RuntimeError):
    """A profile's keys could not be loaded. Message is safe to show."""


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
    keys: tuple[str, ...] = ()  # key-store services injected (dv-0277)

    @classmethod
    def for_profile(cls, profile: Any, working_dir: Path) -> "BashBox":
        """The box of *profile* (None = no profile) rooted at *working_dir*."""
        extra = tuple(getattr(profile, "extra_dirs", ()) or ())
        read = tuple(getattr(profile, "sandbox_read_dirs", ()) or ())
        keys = tuple(getattr(profile, "sandbox_keys", ()) or ())
        return cls(
            write_dirs=(Path(working_dir).resolve(), *extra),
            read_dirs=read,
            keys=keys,
        )

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
        for service in self.keys:
            argv += ["--key", service]
        return shlex.join([*argv, "--", command])

    @staticmethod
    def wrap_env(command: str, names: Sequence[str]) -> str:
        """*command* rewritten to run with the env vars *names* redacted.

        The Codex form (dv-28f2): the values are already in the shell's env
        (the codex child got them), so only the NAMES go on the command line.
        Same quoting contract as :meth:`wrap`: the original command is the
        single word after ``--``.
        """
        argv = [sys.executable, str(Path(__file__).resolve())]
        for name in names:
            argv += ["--redact-env", name]
        return shlex.join([*argv, "--", command])

    @staticmethod
    def unwrap(command: str) -> Optional[str]:
        """The original command inside a launcher line, or None if not one.

        Accepts the bare launcher line and the ``/bin/bash -lc '<line>'`` form
        Codex reports a shell call in, for the progress view and the trace.
        """
        try:
            words = shlex.split(command)
        except ValueError:
            return None
        if (
            len(words) == 3
            and Path(words[0]).name in ("bash", "sh")
            and words[1] in ("-c", "-lc")
        ):
            return BashBox.unwrap(words[2])
        if len(words) < 4 or Path(words[1]).name != Path(__file__).name:
            return None
        if words[-2] != "--":
            return None
        return words[-1]


def load_key_env(services: Sequence[str]) -> "tuple[dict[str, str], List[str]]":
    """(env additions, values to redact, longest first) from the key store.

    Raises :class:`KeyEnvError` on a missing data root, an unreadable or
    empty service. Shared by the launcher (Claude, ``--key``) and the Codex
    backend, which puts the env in its child (dv-28f2). The store module is
    loaded by file path so the launcher stays stdlib-only (as
    ``scripts/withkeys`` does)."""
    import importlib.util

    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from src.utils.paths import data_layout

    spec = importlib.util.spec_from_file_location(
        "_box_keystore", Path(__file__).resolve().parent / "keystore.py"
    )
    assert spec and spec.loader
    ks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ks)
    layout = data_layout()
    if layout.root is None:
        raise KeyEnvError("no data root, keys unavailable")
    store = ks.KeyStore(layout.secrets_dir / "tools")
    env: dict[str, str] = {}
    values: List[str] = []
    for service in services:
        try:
            stored = store.env(service)
        except (ks.KeyStoreError, OSError) as exc:
            raise KeyEnvError(f"key service '{service}': {exc}") from exc
        if not stored:
            raise KeyEnvError(f"no keys stored for '{service}'")
        env.update(stored)
        values += store.secret_values(service)
    return env, sorted(set(values), key=len, reverse=True)


def _key_env(services: Sequence[str]) -> "tuple[dict[str, str], List[str]]":
    """:func:`load_key_env` for the launcher: exits 126 on error.

    Runs outside the box."""
    try:
        return load_key_env(services)
    except KeyEnvError as exc:
        print(f"bash box: {exc}", file=sys.stderr)
        sys.exit(126)


def _redact_env_values(names: Sequence[str]) -> List[str]:
    """Values of the named env vars, longest first; exits 126 if one is unset."""
    values: List[str] = []
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            print(f"bash box: key env {name} missing", file=sys.stderr)
            sys.exit(126)
        if len(value) >= _MIN_REDACT:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def _run_redacted(argv: List[str], env: dict[str, str], values: List[str]) -> int:
    """Run *argv*, stream merged output with every value replaced."""
    import subprocess

    def scrub(line: str) -> str:
        for v in values:
            line = line.replace(v, "<redacted>")
        return line

    proc = subprocess.Popen(
        argv,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(scrub(line))
        sys.stdout.flush()
    return proc.wait()


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Launcher: ``bash_box.py [--cwd D] [--rw D]... [--ro D]... -- COMMAND``,
    or ``bash_box.py --redact-env NAME... -- COMMAND`` (no box, dv-28f2)."""
    parser = argparse.ArgumentParser(prog="bash_box")
    parser.add_argument("--cwd")
    parser.add_argument("--rw", action="append", default=[])
    parser.add_argument("--ro", action="append", default=[])
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--redact-env", action="append", default=[])
    parser.add_argument("command", nargs=1)
    args = parser.parse_args(argv)
    if args.redact_env:
        if args.key or args.rw or args.ro or args.cwd:
            print("bash box: --redact-env takes no box options", file=sys.stderr)
            sys.exit(126)
        values = _redact_env_values(args.redact_env)
        sys.exit(_run_redacted(["/bin/bash", "-c", args.command[0]], {}, values))
    try:
        box = build_bwrap_argv(
            write_dirs=[Path(d) for d in args.rw],
            read_dirs=[Path(d) for d in args.ro],
            chdir=Path(args.cwd) if args.cwd else None,
        )
    except BoxUnavailable as exc:
        print(f"bash box unavailable: {exc}", file=sys.stderr)
        sys.exit(126)
    full = [*box, "--", "/bin/bash", "-c", args.command[0]]
    if args.key:
        env, values = _key_env(args.key)
        sys.exit(_run_redacted(full, env, values))
    os.execv(box[0], full)


if __name__ == "__main__":
    main()
