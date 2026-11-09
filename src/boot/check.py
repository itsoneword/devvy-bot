"""The boot check: does this config + code start? (dv-7d92, dv-86f4).

One checker, two callers, both through ``scripts/check_install.py``:

* install / any time (plain): the same three things ``src/main.py`` does at
  boot, in the same order -- ``load_config``; ``bootstrap_profiles`` then
  ``validate_registry_paths``; ``Storage.initialize`` (creates and migrates
  the schema, seeds ``ALLOWED_USERS`` as owner rows).
* restart preflight (``preflight=True``): the bot runs it in a subprocess
  before ``/restart`` and ``restart_bot`` (``commands/system.py``), and an
  owner-actions step that edits config ends with it. It adds
  ``import src.main`` (a syntax or import error in new code) and replaces
  step 3 with a read-only revision check of all four DBs: the live DBs are
  still in use by the running bot, so nothing here may migrate or create
  them.

Idempotent: everything it touches already refuses to overwrite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

OK = "✔"
BAD = "✘"

# (ok, label, detail) per step.
Result = Tuple[bool, str, str]


def _report(ok: bool, label: str, detail: str = "", quiet: bool = False) -> None:
    if quiet:
        return
    mark = OK if ok else BAD
    line = f"  {mark}  {label}"
    if detail:
        line += f"  --  {detail}"
    print(line)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _sqlite_path(url: str) -> Path:
    """``sqlite:///x.db`` -> ``x.db``, as ``DatabaseManager`` parses it."""
    for prefix in ("sqlite:///", "sqlite://"):
        if url.startswith(prefix):
            return Path(url[len(prefix) :])
    return Path(url)


def db_paths(config: object) -> List[Tuple[str, Path]]:
    """Each of ``migrate.DB_NAMES`` with the path boot uses for it, through
    the owners' own resolvers (Settings, analytics and channel-intel config).
    """
    from src.channel_intel.config import ServerConfig
    from src.llm_analytics.config import AnalyticsConfig

    return [
        ("bot", _sqlite_path(str(getattr(config, "database_url")))),
        ("data", _sqlite_path(str(getattr(config, "user_database_url")))),
        ("llm_analytics", AnalyticsConfig.from_env().db_path),
        ("channel_intel", ServerConfig.from_env().db_path),
    ]


async def verify(
    *,
    skip_db: bool = False,
    env_file: Optional[Path] = None,
    preflight: bool = False,
) -> List[Result]:
    """Run every check. Returns (ok, label, detail) per step; never raises
    for a failed *check* -- only for a bug in the checker itself.

    ``preflight`` prints nothing: the caller prints the first failure first.
    """
    quiet = preflight
    results: List[Result] = []

    def record(ok: bool, label: str, detail: str = "") -> None:
        _report(ok, label, detail, quiet)
        results.append((ok, label, detail))

    # 0. Code (preflight) ------------------------------------------------
    if preflight:
        try:
            import src.main  # noqa: F401
        except BaseException as exc:  # noqa: BLE001 - SyntaxError, SystemExit
            record(False, "code (import src.main)", _first_line(exc))
            return results
        record(True, "code (import src.main)")

    from src.config import load_config

    # 1. Settings -----------------------------------------------------
    try:
        config = load_config(config_file=env_file)
    except Exception as exc:  # noqa: BLE001 - any config error is the message
        record(False, "configuration (.env)", _first_line(exc))
        if not quiet:
            print(
                "\n     Fix .env and re-run. install.sh --dry-run shows what it writes."
            )
        return results
    where = (
        "channel local (no Telegram)"
        if config.channel == "local"
        else f"bot @{config.telegram_bot_username}"
    )
    record(True, "configuration (.env)", where)

    owners = list(config.allowed_users or [])
    if owners:
        record(True, "owner configured", f"ALLOWED_USERS={','.join(map(str, owners))}")
    else:
        # Not fatal here on purpose: the installer can legitimately reach this
        # point with the owner capture skipped or still to come. main.py is
        # where it becomes fatal, and it says so clearly.
        record(False, "owner configured", "ALLOWED_USERS is empty")
        if not quiet:
            print("     The bot will refuse to start. Re-run install.sh, or set")
            print("     ALLOWED_USERS=<your telegram id> in .env.")

    # 2. Profiles -----------------------------------------------------
    registry: Any = None
    if config.profiles_config_path:
        try:
            from src.boot.profiles import bootstrap_profiles
            from src.profiles import load_profile_registry, validate_registry_paths

            registry = load_profile_registry(
                config.profiles_config_path,
                sandbox_default=config.sandbox_enabled,
                data_root=config.devvy_data_root,
            )
            await bootstrap_profiles(registry, REPO_ROOT / "data" / "templates")
            validate_registry_paths(registry)
        except Exception as exc:  # noqa: BLE001
            record(False, "profiles", _first_line(exc))
        else:
            slugs = ", ".join(p.slug for p in registry.profiles)
            record(True, "profiles", f"{len(registry.profiles)} loaded: {slugs}")
    else:
        record(True, "profiles", "single-profile mode (PROFILES_CONFIG_PATH unset)")

    # 2b. pump2 boards (plain only: board health is not boot health, so a
    # red board never blocks the restart gate) --------------------------
    if not preflight:
        try:
            from src.pump2 import cli as pump2_cli
            from src.pump2 import doctor

            boards_file = pump2_cli.DEFAULT_BOARDS
            if not boards_file.exists():
                record(True, "pump2 boards", "not configured (no boards.json)")
            else:
                problems = doctor.check_boards(
                    boards_file,
                    guard=True,
                    registry=registry,
                )
                if problems:
                    more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
                    record(False, "pump2 boards", problems[0] + more)
                else:
                    record(True, "pump2 boards", "every served board is green")
        except Exception as exc:  # noqa: BLE001
            record(False, "pump2 boards", _first_line(exc))

    # 3. Database -----------------------------------------------------
    if skip_db:
        return results
    if preflight:
        try:
            from src.storage.schema.migrate import check_revision

            problems = [
                problem
                for name, path in db_paths(config)
                if (problem := check_revision(name, path))
            ]
        except Exception as exc:  # noqa: BLE001
            record(False, "database", _first_line(exc))
        else:
            if problems:
                record(False, "database", "; ".join(problems))
            else:
                record(True, "database", "every DB opens with this code")
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
        record(False, "database", _first_line(exc))
    else:
        record(True, "database", f"schema ready, {count} owner row(s)")

    return results
