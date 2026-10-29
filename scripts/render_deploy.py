#!/usr/bin/env python3
"""Render deploy/*.template for THIS machine (dv-3b41).

The shipped unit and sudoers files used to be one machine's literal config: a
hardcoded ``User=``, an absolute ``/home/<that login>/...`` working directory
and an absolute poetry path under it. Installing them anywhere else produced a
service that could not start and a sudoers rule naming a user that does not
exist. These are templates now, and this renders them.

Usage:
    python3 scripts/render_deploy.py [--out DIR] [--print] [--service-name NAME]

Every value is detected from the running system; each has an env override:
    DEVVY_ROOT, SERVICE_USER, SERVICE_GROUP, POETRY_BIN, SYSTEMCTL,
    SERVICE_PATH, MEMORY_HIGH, MEMORY_MAX, MEMORY_SWAP_MAX

The rendered sudoers file is validated with ``visudo -cf`` and the renderer
exits non-zero if it does not parse. A malformed file in /etc/sudoers.d can
lock you out of the machine, so this check is not optional.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "deploy"
FIRST_LINE_MARKER = "rendered by scripts/render_deploy.py"


def _detect_user() -> str:
    # SUDO_USER first: `sudo ./install.sh` must configure the invoking user,
    # not root. Running the bot as root is never what anyone wants.
    # SUDO_USER only counts when we actually run as root: after
    # `sudo -iu someone` it still names the *previous* user, and trusting it
    # would install the service as the wrong account.
    override = os.environ.get("SERVICE_USER")
    if override and override != "root":
        return override
    sudo_user = os.environ.get("SUDO_USER")
    if os.getuid() == 0 and sudo_user and sudo_user != "root":
        return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def _detect_group(user: str) -> str:
    override = os.environ.get("SERVICE_GROUP")
    if override:
        return override
    try:
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    except KeyError:
        return user


def _which(name: str, fallback: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    # A login shell's ~/.local/bin is often absent from a non-interactive PATH.
    candidate = Path.home() / ".local" / "bin" / name
    if candidate.exists():
        return str(candidate)
    return fallback


def build_values(service_name: str) -> dict[str, str]:
    user = _detect_user()
    root = Path(os.environ.get("DEVVY_ROOT") or REPO_ROOT)
    poetry = os.environ.get("POETRY_BIN") or _which("poetry", "/usr/local/bin/poetry")
    systemctl = os.environ.get("SYSTEMCTL") or _which("systemctl", "/usr/bin/systemctl")
    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        home = Path.home()
    default_path = os.pathsep.join(
        [
            str(home / ".local" / "bin"),
            str(home / ".npm-global" / "bin"),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
    )
    return {
        "SERVICE_USER": user,
        "SERVICE_GROUP": _detect_group(user),
        "SERVICE_NAME": service_name,
        "DEVVY_ROOT": str(root),
        "POETRY_BIN": poetry,
        "SYSTEMCTL": systemctl,
        "SERVICE_PATH": os.environ.get("SERVICE_PATH") or default_path,
        "MEMORY_HIGH": os.environ.get("MEMORY_HIGH", "2G"),
        "MEMORY_MAX": os.environ.get("MEMORY_MAX", "2.5G"),
        "MEMORY_SWAP_MAX": os.environ.get("MEMORY_SWAP_MAX", "256M"),
    }


def render(template: Path, values: dict[str, str]) -> str:
    text = template.read_text(encoding="utf-8")
    # Drop the "do not install this directly" banner from the unit file, which
    # is not a comment in systemd syntax.
    lines = text.splitlines(keepends=True)
    if lines and FIRST_LINE_MARKER in lines[0] and not lines[0].startswith("#"):
        lines = lines[1:]
    text = "".join(lines)
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    leftover = [
        tok
        for tok in ("@SERVICE_USER@", "@DEVVY_ROOT@", "@POETRY_BIN@", "@SYSTEMCTL@")
        if tok in text
    ]
    if leftover:
        raise SystemExit(f"{template.name}: unsubstituted placeholders {leftover}")
    return text


def validate_sudoers(path: Path) -> None:
    """Hard gate. A sudoers file that does not parse can lock you out."""
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    if not Path(visudo).exists():
        raise SystemExit(
            f"visudo not found at {visudo}; refusing to emit an unvalidated"
            " sudoers file. Install the sudo package, or render with --print"
            " and validate by hand."
        )
    proc = subprocess.run([visudo, "-cf", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"rendered sudoers failed validation ({visudo} -cf):\n"
            f"{proc.stdout}{proc.stderr}"
        )
    print(f"  validated: {proc.stdout.strip() or 'parsed OK'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, help="directory to write rendered files into")
    ap.add_argument("--print", action="store_true", help="write to stdout instead")
    ap.add_argument("--service-name", default="claude-bot")
    args = ap.parse_args()

    values = build_values(args.service_name)
    templates = sorted(TEMPLATE_DIR.glob("*.template"))
    if not templates:
        raise SystemExit(f"no templates found in {TEMPLATE_DIR}")

    if args.print:
        for tpl in templates:
            print(f"--- {tpl.name} ---")
            print(render(tpl, values))
        return 0

    out_dir = args.out or (REPO_ROOT / "deploy" / "rendered")
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Rendering with:")
    for key in sorted(values):
        print(f"  {key} = {values[key]}")
    print()

    for tpl in templates:
        target = out_dir / tpl.name[: -len(".template")]
        # Remove first: the sudoers file we wrote last time is mode 0440, so
        # opening it for writing raises PermissionError and the SECOND run of
        # install.sh dies here with a traceback. Rendering is idempotent by
        # definition -- the output is a pure function of the templates and this
        # machine -- so replacing the file is always safe.
        target.unlink(missing_ok=True)
        target.write_text(render(tpl, values), encoding="utf-8")
        # Sudoers must be 0440 wherever it lives; the mode travels with `cp -p`.
        target.chmod(0o440 if "sudoers" in target.name else 0o644)
        print(f"wrote {target}")
        if "sudoers" in target.name:
            validate_sudoers(target)

    print(f"\nRendered into {out_dir}. Install with:")
    print(
        f"  sudo cp {out_dir}/claude-bot.service /etc/systemd/system/"
        f"{args.service_name}.service"
    )
    print(
        f"  sudo cp {out_dir}/sudoers-claude-bot /etc/sudoers.d/" f"{args.service_name}"
    )
    print(f"  sudo chmod 440 /etc/sudoers.d/{args.service_name}")
    print("  sudo systemctl daemon-reload")
    print("User timers (opt-in, docs/systemd-operations.md):")
    print(
        f"  cp {out_dir}/tmpsweep.service {out_dir}/tmpsweep.timer ~/.config/systemd/user/"
    )
    print(
        "  systemctl --user daemon-reload && systemctl --user enable --now tmpsweep.timer"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
