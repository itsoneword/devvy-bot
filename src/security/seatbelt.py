"""The two macOS boxes: Seatbelt profiles for ``sandbox-exec`` (dv-fe4d).

macOS has no bwrap. Its box tool is ``sandbox-exec -p <profile>`` (deprecated,
but it is what the Claude CLI's own native sandbox uses there). This module
renders the profile text for the two boxes the Linux host builds with bwrap;
stdlib only, like ``bash_box.py``, because both launchers import it.

* :func:`job_profile` mirrors ``scripts/bgjob``'s ``job_box_prefix``: reads
  open everywhere (as ``--ro-bind / /``), writes only on the job's paths. The
  Linux box also hides the user bus because ``systemd-run --user`` escaped it
  (dv-5e2f); the macOS form of that escape is launchd (``launchctl submit``),
  LaunchServices (``open``) and AppleEvents (``osascript``). Closed twice: the
  profile is ``(deny default)`` with ``mach-lookup`` limited to
  :data:`MACH_ALLOW` (none of :data:`MACH_DENY_ALWAYS`), and exec of the
  escape binaries is denied after the exec allow, so it fails with EPERM.
* :func:`bash_profile` mirrors ``bash_box.build_bwrap_argv``: system dirs
  readable, nothing under ``$HOME`` except ``_HOME_RO`` and the read/write
  grants, writes only on the write grants and the CLI scratch dir.

Known differences from bwrap, stated rather than hidden: there is no PID or
IPC namespace (approximated by ``process-info*`` / ``signal`` limited to the
same sandbox). In the chat box, file metadata outside ``$HOME`` is readable;
under ``$HOME`` only ``$HOME`` itself and the ancestors of the grants can be
stat'ed (so realpath/getcwd resolve a granted dir), and anything else under
it, secret dirs included, can be neither stat'ed nor opened. Exec of the
escape binaries is denied by path only: a copy in a writable dir still
runs, so the real guard against ``launchctl submit`` is launchd refusing a
sandboxed caller. That, and every rule here, is unverified on this (Linux)
host: ``scripts/seatbelt_probe`` checks them on the Mac.

Seatbelt applies the LAST matching rule, so every deny that narrows an
allow sits after it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable, List, Sequence


def _bash_box() -> ModuleType:
    """bash_box.py, the chat box's single source of ``_HOME_RO`` and the
    scratch dir. Loaded by file path when the package is not imported, so
    scripts/bgjob (system python, no deps) can load this module standalone."""
    name = "_seatbelt_bash_box"
    mod = sys.modules.get("src.security.bash_box") or sys.modules.get(name)
    if mod is not None:
        return mod
    path = Path(__file__).resolve().parent / "bash_box.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: bash_box declares a dataclass, and dataclasses
    # looks its module up in sys.modules while the class body runs.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


_HOME_RO: tuple[str, ...] = tuple(_bash_box()._HOME_RO)

# Mach services a boxed process may look up. The Keychain/TLS three are what
# the Claude CLI's login needs; the rest are libc plumbing (user lookup, logs,
# per-user temp dirs, DNS, preferences). Structure copied from the CLI's own
# macOS profile, minus its opt-in LaunchServices/AppleEvents block.
MACH_ALLOW: tuple[str, ...] = (
    "com.apple.SecurityServer",
    "com.apple.securityd.xpc",
    "com.apple.trustd.agent",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.logd",
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.bsd.dirhelper",
    "com.apple.dnssd.service",
    "com.apple.SystemConfiguration.configd",
    "com.apple.SystemConfiguration.DNSConfiguration",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
)

# Never in a profile: these are what ``open`` and ``osascript`` talk to.
MACH_DENY_ALWAYS: tuple[str, ...] = (
    "com.apple.coreservices.launchservicesd",
    "com.apple.lsd.mapdb",
    "com.apple.lsd.modifydb",
    "com.apple.coreservices.appleevents",
    "com.apple.CoreServices.coreservicesd",
    "com.apple.coreservices.quarantine-resolver",
)

# Exec of these fails even though exec is otherwise allowed: launchd itself
# is reached through the bootstrap port, not a named service, so the
# mach-lookup list alone does not stop ``launchctl submit``.
ESCAPE_BINARIES: tuple[str, ...] = (
    "/bin/launchctl",
    "/usr/bin/open",
    "/usr/bin/osascript",
    "/usr/bin/shortcuts",
    "/usr/bin/automator",
)

# What the chat box may read outside $HOME (bwrap's _SYSTEM_RO, macOS names).
_SYSTEM_READ = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/opt",
    "/private/etc",
    "/private/var/db/timezone",
    "/dev",
)
_DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty")
# Shared temp roots a job may write (its TMPDIR is its job dir, dv-1a02, but
# tools still fall back to these).
_JOB_TMP = ("/private/tmp", "/private/var/folders")


def _q(path: str) -> str:
    """*path* as a Seatbelt string literal."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _variants(paths: Iterable[str]) -> List[str]:
    """Each path, plus its realpath when that differs (``/tmp`` is
    ``/private/tmp`` on macOS, and Seatbelt matches resolved paths)."""
    out: List[str] = []
    for raw in paths:
        for p in (str(raw), os.path.realpath(str(raw))):
            if p not in out:
                out.append(p)
    return out


def _base() -> List[str]:
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(deny process-exec "
        + " ".join(f"(literal {_q(b)})" for b in ESCAPE_BINARIES)
        + ")",
        "(allow process-info* (target same-sandbox))",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow ipc-posix-sem)",
        "(allow ipc-posix-shm)",
        "(allow pseudo-tty)",
        "(allow user-preference-read)",
        "(allow file-read-metadata)",
        "(allow file-ioctl "
        + " ".join(f"(literal {_q(d)})" for d in _DEVICES)
        + ' (regex #"^/dev/ttys[0-9]+$") (literal "/dev/ptmx"))',
        # Not boxed on Linux either: jobs and chat reach the API and the
        # bot's 127.0.0.1 internal API.
        "(allow network*)",
    ]
    lines += [f"(allow mach-lookup (global-name {_q(s)}))" for s in MACH_ALLOW]
    return lines


def _writes(paths: Sequence[str]) -> List[str]:
    lines = [f"(allow file-write* (subpath {_q(p)}))" for p in _variants(paths)]
    lines += [f"(allow file-write* (literal {_q(d)}))" for d in _DEVICES]
    lines.append('(allow file-write* (regex #"^/dev/ttys[0-9]+$"))')
    return lines


def _hide(paths: Sequence[str]) -> List[str]:
    """Deny every access to *paths* (files or dirs), whatever was allowed
    before: the last rule wins, so these go at the very end."""
    return [
        f"(deny file-read* file-write* (subpath {_q(p)}))" for p in _variants(paths)
    ]


def job_profile(write_paths: Sequence[str], hidden: Sequence[str] = ()) -> str:
    """The job box: read anything but *hidden* (the local TUI's credential,
    dv-fe4d), write only *write_paths* (+ temp roots)."""
    lines = _base()
    lines.append("(allow file-read*)")
    lines += _writes([*write_paths, *_JOB_TMP])
    lines += _hide(hidden)
    return "\n".join(lines) + "\n"


def bash_profile(
    write_dirs: Sequence[str],
    read_dirs: Sequence[str] = (),
    home: str | None = None,
    hidden: Sequence[str] = (),
) -> str:
    """The chat Bash box: system dirs readable, ``$HOME`` closed except
    ``_HOME_RO`` and the grants, writes only on *write_dirs* + scratch.
    *hidden* (the secrets dir, the TUI credential) stays closed even when a
    grant contains it."""
    home = str(home or Path.home())
    writes = [*map(str, write_dirs), str(_bash_box().scratch_dir())]
    lines = _base()
    lines += [f"(allow file-read* (subpath {_q(p)}))" for p in _SYSTEM_READ]
    lines.append(f"(deny file-read* (subpath {_q(home)}))")
    # Re-opened after the deny, in bwrap's order: home tools, read grants,
    # then write grants (which are readable too).
    reopen = [f"{home}/{rel}" for rel in _HOME_RO]
    reopen += [*map(str, read_dirs), *writes]
    reopen = _variants(reopen)
    lines += [f"(allow file-read* (subpath {_q(p)}))" for p in reopen]
    # The deny also took file-read-metadata. Give back stat (only) on $HOME
    # and on each grant's ancestors under it, so realpath/getcwd resolve a
    # granted dir; a sibling secret dir stays unstat-able, as under bwrap's
    # tmpfs $HOME where it does not exist.
    lines += [
        f"(allow file-read-metadata (literal {_q(p)}))"
        for p in _home_ancestors(reopen, _variants([home]))
    ]
    lines += _writes(writes)
    lines += _hide(hidden)
    return "\n".join(lines) + "\n"


def _home_ancestors(paths: Sequence[str], homes: Sequence[str]) -> List[str]:
    """Each home in *homes* plus every directory between it and a path in
    *paths* below it (the path itself excluded: its subpath is readable)."""
    out: List[str] = []
    for home in homes:
        out.append(home)
        for p in paths:
            rel = os.path.relpath(p, home)
            if rel == "." or rel.startswith(".."):
                continue
            parts = Path(rel).parts[:-1]
            for i in range(1, len(parts) + 1):
                anc = os.path.join(home, *parts[:i])
                if anc not in out:
                    out.append(anc)
    return out


def sandbox_exec_argv(profile: str, sandbox_exec: str = "sandbox-exec") -> List[str]:
    """The argv prefix (up to the command) that runs a command in *profile*."""
    return [sandbox_exec, "-p", profile]
