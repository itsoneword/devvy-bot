#!/usr/bin/env python3
"""Post-install verification, run inside the project venv (dv-7d92 steps 6-7).

Everything before this in ``install.sh`` is stdlib-only text manipulation. This
is the first step that loads the *real* code, and that is the point: it proves
the config the installer just wrote actually parses, that the profiles resolve
to real directories, and that the database schema exists -- at install time,
where the message is on screen, rather than three seconds into the first
service start where it is a journal entry nobody reads.

It does the same three things ``src/main.py`` does at boot, in the same order:

1. ``load_config`` -- Settings, including the required token/username.
2. ``bootstrap_profiles`` then ``validate_registry_paths`` -- materialise every
   profile's workspace, memory dir and persona from templates, then check them.
3. ``Storage.initialize`` -- create the schema, and seed ``ALLOWED_USERS`` as
   owner rows so the owner exists in the DB and not only in the environment.

Idempotent: everything it touches already refuses to overwrite. Run it any time
to check a deployment, not just from the installer.

The checks themselves live in ``src/boot/check.py``. ``--preflight`` is the
restart gate (dv-86f4): the bot runs it before ``/restart`` and
``restart_bot``, and an owner-actions step that edits config ends with it. It
never creates or migrates a DB.

Usage:
    poetry run python scripts/check_install.py
    poetry run python scripts/check_install.py --skip-db
    poetry run python scripts/check_install.py --preflight
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.boot.check import BAD, OK, verify  # noqa: E402


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--skip-db",
        action="store_true",
        help="config and profiles only; do not create or open the database",
    )
    p.add_argument(
        "--preflight",
        action="store_true",
        help="restart preflight (dv-86f4): also import the code, and check the "
        "DBs read-only instead of migrating them; exit 0/1, first line = the "
        "first problem",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        help="path to the .env to check (default: ./.env, as the service uses)",
    )
    return p.parse_args(argv)


def preflight(skip_db: bool = False, env_file: Optional[Path] = None) -> int:
    """The restart gate: the first failure is the first line on stdout (the
    bot quotes it when it refuses a restart), then every step."""
    # Library logging (structlog prints to stdout) must not come first.
    with contextlib.redirect_stdout(sys.stderr):
        results = asyncio.run(
            verify(skip_db=skip_db, env_file=env_file, preflight=True)
        )
    failed = [(label, detail) for ok, label, detail in results if not ok]
    if failed:
        label, detail = failed[0]
        print(f"{BAD} {label}: {detail}" if detail else f"{BAD} {label}")
    else:
        print(f"{OK} boot check passed")
    for ok, label, detail in results:
        line = f"  {OK if ok else BAD}  {label}"
        print(f"{line}  --  {detail}" if detail else line)
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.preflight:
        return preflight(skip_db=args.skip_db, env_file=args.env_file)
    print("\nVerifying the installed configuration")
    print("-------------------------------------")
    results = asyncio.run(verify(skip_db=args.skip_db, env_file=args.env_file))
    failed = [label for ok, label, _detail in results if not ok]
    print()
    if failed:
        print(f"{BAD} not ready: {', '.join(failed)}")
        return 1
    print(f"{OK} configuration, profiles and database are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
