#!/usr/bin/env python3
"""Preflight dependency check for install.sh (dv-7d92 step 1).

Prints one table: every dependency, whether it is there, what version was
found, and -- for anything missing -- the exact command that fixes it. Nothing
is installed here. ``install.sh`` reads ``--json`` and offers to run the fix
commands one at a time, because a script that silently ``apt install``s is the
kind of installer people refuse to run.

Standard library only: this is the very first thing the installer does, before
Poetry exists, let alone the project's virtualenv.

Usage:
    preflight.py                # human table, exit 1 if a required dep is missing
    preflight.py --json         # machine-readable, same exit code
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

# Matches the first dotted version in a --version banner: "v22.22.2",
# "Poetry (version 2.3.2)", "2.1.257 (Claude Code)", "systemd 255 (255.4-1)".
VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")

MIN_PYTHON = (3, 11)  # pyproject: requires-python = ">=3.11"
MIN_NODE = (18,)  # the claude CLI is a Node app


@dataclass
class Check:
    """One dependency and how to get it."""

    key: str
    label: str
    why: str
    fix: str
    required: bool = True
    # A command install.sh may OFFER to run. None = we will not automate it.
    autofix: Optional[str] = None
    autofix_needs_sudo: bool = False


@dataclass
class Result:
    check: Check
    ok: bool
    found: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.check.key,
            "label": self.check.label,
            "ok": self.ok,
            "required": self.check.required,
            "found": self.found,
            "note": self.note,
            "fix": "" if self.ok else self.check.fix,
            "autofix": "" if self.ok else (self.check.autofix or ""),
            "autofix_needs_sudo": self.check.autofix_needs_sudo,
            "why": self.check.why,
        }


# --- primitives ---------------------------------------------------------


def run_version(cmd: Sequence[str]) -> Optional[str]:
    """First line of ``cmd``'s output, or None if it cannot be run.

    Every failure is None on purpose: a dependency that errors, hangs past the
    timeout or is not on PATH is equally "not usable", and preflight's job is
    to say so, not to explain execve.
    """
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out.splitlines()[0].strip() if out else None


def parse_version(text: str) -> Tuple[int, ...]:
    """Leading dotted number in a version banner, as a tuple. () if none."""
    match = VERSION_RE.search(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def at_least(found: Tuple[int, ...], minimum: Tuple[int, ...]) -> bool:
    """Compare version tuples, ignoring components the minimum does not name."""
    if not found:
        return False
    return found[: len(minimum)] >= minimum


def fmt_version(v: Tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)


# --- the checks ---------------------------------------------------------


def check_os(check: Check) -> Result:
    system = platform.system()
    found = f"{system} {platform.machine()}"
    if system != "Linux":
        return Result(
            check,
            False,
            found,
            "v1 targets Linux + systemd only; see the task notes for macOS.",
        )
    return Result(check, True, found)


def check_systemd(check: Check) -> Result:
    banner = run_version(["systemctl", "--version"])
    if banner is None:
        return Result(check, False, "", "systemctl not on PATH")
    # A systemctl binary is not the same as systemd being PID 1: containers and
    # WSL1 ship the client and nothing to talk to, and that failure would
    # otherwise only surface at `systemctl enable`.
    running = os.path.exists("/run/systemd/system")
    if not running:
        return Result(check, False, banner, "systemd is not the init system here")
    return Result(check, True, banner)


def check_python(check: Check) -> Result:
    found = sys.version_info[:3]
    ok = at_least(found, MIN_PYTHON)
    note = "" if ok else f"need >= {fmt_version(MIN_PYTHON)}"
    return Result(check, ok, fmt_version(found), note)


def _versioned(check: Check, cmd: Sequence[str], minimum: Tuple[int, ...]) -> Result:
    banner = run_version(cmd)
    if banner is None:
        return Result(check, False, "", f"{cmd[0]} not on PATH")
    version = parse_version(banner)
    if minimum and not at_least(version, minimum):
        return Result(check, False, banner, f"need >= {fmt_version(minimum)}")
    return Result(check, True, banner)


def check_node(check: Check) -> Result:
    return _versioned(check, ["node", "--version"], MIN_NODE)


def _present(check: Check, cmd: Sequence[str]) -> Result:
    banner = run_version(cmd)
    if banner is None:
        return Result(check, False, "", f"{cmd[0]} not on PATH")
    return Result(check, True, banner)


def check_poetry(check: Check) -> Result:
    return _present(check, ["poetry", "--version"])


def check_git(check: Check) -> Result:
    return _present(check, ["git", "--version"])


def check_claude(check: Check) -> Result:
    return _present(check, ["claude", "--version"])


def check_bwrap(check: Check) -> Result:
    path = shutil.which("bwrap")
    if not path:
        return Result(check, False, "", "background jobs will not run without it")
    return Result(check, True, path)


def check_sudo(check: Check) -> Result:
    path = shutil.which("sudo")
    if not path:
        return Result(check, False, "", "needed to install the unit and sudoers rule")
    return Result(check, True, path)


def check_visudo(check: Check) -> Result:
    path = shutil.which("visudo") or (
        "/usr/sbin/visudo" if os.path.exists("/usr/sbin/visudo") else ""
    )
    if not path:
        return Result(check, False, "", "the sudoers rule cannot be validated")
    return Result(check, True, path)


@dataclass
class Spec:
    check: Check
    probe: Callable[[Check], Result] = field(repr=False)


def build_specs() -> List[Spec]:
    """Every dependency, in the order the table prints them."""
    return [
        Spec(
            Check(
                "os",
                "Linux",
                "systemd unit, sudoers rule and bubblewrap sandbox are Linux-only",
                "Devvy v1 installs on Linux with systemd. No macOS/Windows path yet.",
            ),
            check_os,
        ),
        Spec(
            Check(
                "systemd",
                "systemd",
                "the bot runs as a systemd service; bgjob uses systemd --user",
                "Install a systemd-based distro, or run the bot by hand:"
                " poetry run python -m src.main",
            ),
            check_systemd,
        ),
        Spec(
            Check(
                "python",
                f"Python >= {fmt_version(MIN_PYTHON)}",
                "the project targets 3.11+",
                "sudo apt-get install -y python3 python3-venv python3-pip",
                autofix="sudo apt-get update"
                " && sudo apt-get install -y python3 python3-venv python3-pip",
                autofix_needs_sudo=True,
            ),
            check_python,
        ),
        Spec(
            Check(
                "git",
                "git",
                "the bot updates itself from git",
                "sudo apt-get install -y git",
                autofix="sudo apt-get update && sudo apt-get install -y git",
                autofix_needs_sudo=True,
            ),
            check_git,
        ),
        Spec(
            Check(
                "poetry",
                "Poetry",
                "dependency install and the service's ExecStart",
                "curl -sSL https://install.python-poetry.org | python3 -",
                autofix="curl -sSL https://install.python-poetry.org | python3 -",
            ),
            check_poetry,
        ),
        Spec(
            Check(
                "node",
                f"Node >= {fmt_version(MIN_NODE)}",
                "the claude CLI is a Node application",
                "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -"
                " && sudo apt-get install -y nodejs",
                autofix="curl -fsSL https://deb.nodesource.com/setup_22.x"
                " | sudo -E bash - && sudo apt-get install -y nodejs",
                autofix_needs_sudo=True,
            ),
            check_node,
        ),
        Spec(
            Check(
                "claude",
                "claude CLI",
                "every agent turn runs through it",
                "curl -fsSL https://claude.ai/install.sh | bash",
                autofix="curl -fsSL https://claude.ai/install.sh | bash",
            ),
            check_claude,
        ),
        Spec(
            Check(
                "bwrap",
                "bubblewrap",
                "scripts/bgjob refuses to start background jobs without it",
                "sudo apt-get install -y bubblewrap",
                required=False,
                autofix="sudo apt-get update && sudo apt-get install -y bubblewrap",
                autofix_needs_sudo=True,
            ),
            check_bwrap,
        ),
        Spec(
            Check(
                "sudo",
                "sudo",
                "installs the systemd unit and the /restart sudoers rule",
                "Install sudo, or install the unit and sudoers file by hand as root"
                " (install.sh --dry-run prints both).",
            ),
            check_sudo,
        ),
        Spec(
            Check(
                "visudo",
                "visudo",
                "validates the sudoers rule before it is installed",
                "sudo apt-get install -y sudo",
                required=False,
                autofix="sudo apt-get update && sudo apt-get install -y sudo",
                autofix_needs_sudo=True,
            ),
            check_visudo,
        ),
    ]


def run_checks(specs: Optional[Sequence[Spec]] = None) -> List[Result]:
    return [spec.probe(spec.check) for spec in (specs or build_specs())]


def missing_required(results: Sequence[Result]) -> List[Result]:
    return [r for r in results if not r.ok and r.check.required]


# --- output -------------------------------------------------------------

TICK = "✔"
CROSS = "✘"
WARN = "!"


def render_table(results: Sequence[Result]) -> str:
    """The ✔/✘ table, plus a fix command per failure."""
    width = max(len(r.check.label) for r in results)
    lines = ["", "Preflight", "---------"]
    for r in results:
        mark = TICK if r.ok else (CROSS if r.check.required else WARN)
        detail = r.found or r.note
        if r.found and r.note:
            detail = f"{r.found}  ({r.note})"
        lines.append(f"  {mark}  {r.check.label:<{width}}  {detail}")

    failures = [r for r in results if not r.ok]
    if failures:
        lines.append("")
        for r in failures:
            kind = "MISSING" if r.check.required else "optional"
            lines.append(f"  {kind}: {r.check.label} -- {r.check.why}")
            lines.append(f"    fix: {r.check.fix}")
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing; use the exit code (0 = every required dep present)",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    results = run_checks()
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    elif not args.quiet:
        sys.stdout.write(render_table(results))
    return 1 if missing_required(results) else 0


if __name__ == "__main__":
    sys.exit(main())
