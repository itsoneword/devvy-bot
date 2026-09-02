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

Usage:
    poetry run python scripts/check_install.py
    poetry run python scripts/check_install.py --skip-db
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OK = "✔"
BAD = "✘"


class CheckFailed(RuntimeError):
    """One verification step failed; the message is meant for a human."""


def _report(ok: bool, label: str, detail: str = "") -> None:
    mark = OK if ok else BAD
    line = f"  {mark}  {label}"
    if detail:
        line += f"  --  {detail}"
    print(line)


async def verify(
    *, skip_db: bool = False, env_file: Optional[Path] = None
) -> List[Tuple[bool, str]]:
    """Run every check. Returns (ok, label) per step; never raises for a
    failed *check* -- only for a bug in the checker itself."""
    from src.config import load_config

    results: List[Tuple[bool, str]] = []

    # 1. Settings -----------------------------------------------------
    try:
        config = load_config(config_file=env_file)
    except Exception as exc:  # noqa: BLE001 - any config error is the message
        _report(False, "configuration (.env)", str(exc).splitlines()[0])
        print("\n     Fix .env and re-run. install.sh --dry-run shows what it writes.")
        return [(False, "configuration")]
    _report(True, "configuration (.env)", f"bot @{config.telegram_bot_username}")
    results.append((True, "configuration"))

    owners = list(config.allowed_users or [])
    if owners:
        _report(True, "owner configured", f"ALLOWED_USERS={','.join(map(str, owners))}")
        results.append((True, "owner"))
    else:
        # Not fatal here on purpose: the installer can legitimately reach this
        # point with the owner capture skipped or still to come. main.py is
        # where it becomes fatal, and it says so clearly.
        _report(False, "owner configured", "ALLOWED_USERS is empty")
        print("     The bot will refuse to start. Re-run install.sh, or set")
        print("     ALLOWED_USERS=<your telegram id> in .env.")
        results.append((False, "owner"))

    # 2. Profiles -----------------------------------------------------
    if config.profiles_config_path:
        try:
            from src.boot.profiles import bootstrap_profiles
            from src.profiles import load_profile_registry, validate_registry_paths

            registry = load_profile_registry(
                config.profiles_config_path,
                sandbox_default=config.sandbox_enabled,
            )
            await bootstrap_profiles(registry, REPO_ROOT / "data" / "templates")
            validate_registry_paths(registry)
        except Exception as exc:  # noqa: BLE001
            _report(False, "profiles", str(exc).splitlines()[0])
            results.append((False, "profiles"))
        else:
            slugs = ", ".join(p.slug for p in registry.profiles)
            _report(True, "profiles", f"{len(registry.profiles)} loaded: {slugs}")
            results.append((True, "profiles"))
    else:
        _report(True, "profiles", "single-profile mode (PROFILES_CONFIG_PATH unset)")
        results.append((True, "profiles"))

    # 3. Database -----------------------------------------------------
    if skip_db:
        return results
    try:
        from src.storage.facade import Storage
        from src.storage.user_repository import UserStore

        storage = Storage(
            config.database_url,
            tasks_dir=config.deviz_tasks_dir,
            user_database_url=config.user_database_url,
        )
        await storage.initialize()
        try:
            user_store = UserStore(storage.bot_users)
            await user_store.seed_env_owners(owners)
            await user_store.refresh()
            count = len(user_store.owner_ids())
        finally:
            close = getattr(storage, "close", None)
            if close is not None:
                await close()
    except Exception as exc:  # noqa: BLE001
        _report(False, "database", str(exc).splitlines()[0])
        results.append((False, "database"))
    else:
        _report(True, "database", f"schema ready, {count} owner row(s)")
        results.append((True, "database"))

    return results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--skip-db",
        action="store_true",
        help="config and profiles only; do not create or open the database",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        help="path to the .env to check (default: ./.env, as the service uses)",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    print("\nVerifying the installed configuration")
    print("-------------------------------------")
    results = asyncio.run(verify(skip_db=args.skip_db, env_file=args.env_file))
    failed = [label for ok, label in results if not ok]
    print()
    if failed:
        print(f"{BAD} not ready: {', '.join(failed)}")
        return 1
    print(f"{OK} configuration, profiles and database are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
