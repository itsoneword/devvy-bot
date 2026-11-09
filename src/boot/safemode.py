"""Safe mode: boot devops only from the last-known-good config (dv-86f4).

State lives in ``DataLayout.lkg_dir`` (``<data root>/lkg``):

- ``profiles.yaml`` / ``models.yaml`` / ``meta.json``: the config of the last
  boot that ran ``SNAPSHOT_DELAY_S`` with polling up AND a clean restart
  preflight (:func:`snapshot_after`, scheduled by ``src/main.py``).
- ``USE``: set by ``deploy/crashwatch.sh`` on a crash loop when a snapshot
  exists. The next boot loads profiles/models from the snapshot
  (:func:`config_paths`), keeps only the default profile, owner-only
  (:func:`apply`), and tells the owner. A ``/restart`` whose preflight passes
  on the LIVE config removes it (:func:`leave`).
- ``tried`` / ``HALT``: crashwatch's own: safe mode was tried since the last
  :func:`leave`; safe mode crashed too (or a loop found no way into it: no
  snapshot, or already tried), so systemd must not start the bot again (``ExecCondition=``) until the owner runs ``crashwatch.sh reset``.

Only the default profile runs because an old config may carry grants revoked
since (sandbox, extra_dirs, roles, ...): devops is the agent that can fix the
rest (owner, 2026-10-31, answer 4.2). Config only, not old code: old code
cannot open a DB already migrated forward (answer 4.1).
"""

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple

import structlog

from src.profiles import ProfileRegistry, load_profile_registry
from src.utils.paths import REPO_ROOT, DataLayout

logger = structlog.get_logger()

# How long a boot must run healthy before its config becomes the snapshot.
SNAPSHOT_DELAY_S = 600.0
SAFE_MODE_REASON = "safe mode"


def _lkg(layout: DataLayout) -> Path:
    return Path(layout.lkg_dir)


def is_active(layout: DataLayout) -> bool:
    """Crashwatch asked for a safe-mode boot."""
    return (_lkg(layout) / "USE").exists()


def config_paths(
    layout: DataLayout,
    profiles_path: Optional[Path],
    models_path: Optional[Path],
) -> Tuple[Optional[Path], Optional[Path]]:
    """The profiles/models files this boot loads: the snapshot copies in safe
    mode (each only if it was snapshotted), else the given live paths."""
    if not is_active(layout):
        return profiles_path, models_path
    lkg = _lkg(layout)
    snap_profiles = lkg / "profiles.yaml"
    snap_models = lkg / "models.yaml"
    return (
        snap_profiles if profiles_path and snap_profiles.exists() else profiles_path,
        snap_models if snap_models.exists() else models_path,
    )


def leave(layout: DataLayout) -> None:
    """Back to the live config on the next boot. Called after a ``/restart``
    whose preflight passed on the live config; also re-arms safe mode for the
    next crash loop (crashwatch tries it once per :func:`leave`)."""
    lkg = _lkg(layout)
    for name in ("USE", "tried"):
        try:
            (lkg / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Safe mode flag not removed", flag=name, error=str(e))
    logger.info("Safe mode left (next boot uses the live config)")


def head_sha() -> str:
    """Short git sha of the running checkout, "" when unknown."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


async def snapshot_after(
    layout: DataLayout,
    *,
    profiles_path: Optional[Path],
    models_path: Optional[Path],
    check: Callable[[], Awaitable[Optional[str]]],
    delay: float = SNAPSHOT_DELAY_S,
    sha: str = "",
) -> bool:
    """After ``delay`` s up, save the live config as last-known-good.

    Only when ``check()`` (the restart preflight: None = clean) passes, so a
    config that would not boot again is never the fallback. Cancelled before
    the delay ends (the boot died or was restarted) = nothing written.
    """
    await asyncio.sleep(delay)
    if profiles_path is None or not Path(profiles_path).is_file():
        return False
    problem = await check()
    if problem:
        logger.info("Config snapshot skipped: boot check failed", reason=problem)
        return False
    lkg = _lkg(layout)
    try:
        lkg.mkdir(parents=True, exist_ok=True)
        _copy(Path(profiles_path), lkg / "profiles.yaml")
        if models_path is not None and Path(models_path).is_file():
            _copy(Path(models_path), lkg / "models.yaml")
        else:
            (lkg / "models.yaml").unlink(missing_ok=True)
        meta = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sha": sha,
            "profiles": str(profiles_path),
        }
        (lkg / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    except OSError as e:
        logger.warning("Config snapshot failed", error=str(e))
        return False
    logger.info("Config snapshot saved", dir=str(lkg), sha=sha)
    return True


def _meta(layout: DataLayout) -> dict:
    try:
        data = json.loads((_lkg(layout) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _devops_diff(
    registry: ProfileRegistry, live_profiles_path: Optional[Path], **load: Any
) -> str:
    """How the snapshot's default profile differs from the live file's."""
    snap = registry.get_default()
    if live_profiles_path is None:
        return "живого profiles.yaml нет"
    try:
        live_reg = load_profile_registry(Path(live_profiles_path), **load)
    except Exception as e:  # noqa: BLE001 - any parse error is the message
        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return f"живой profiles.yaml не читается: {first[:200]}"
    live = live_reg.get_default()
    if live.slug != snap.slug:
        return f"в живом конфиге профиль по умолчанию другой: {live.slug}"
    changed = [
        f.name for f in fields(snap) if getattr(snap, f.name) != getattr(live, f.name)
    ]
    if not changed:
        return f"{snap.slug} совпадает с живым"
    return f"{snap.slug} в живом конфиге отличается: {', '.join(changed)}"


def apply(
    registry: ProfileRegistry,
    layout: DataLayout,
    live_profiles_path: Optional[Path],
    *,
    models_from_snapshot: bool = False,
    **load: Any,
) -> str:
    """Narrow the snapshot registry to safe mode; return the owner message.

    Every non-default profile goes off for this run (phase 2's
    ``disable_for_run``, so routing, /agents and the menus already honour
    it), the default profile becomes owner-only. ``load`` is passed to
    ``load_profile_registry`` for the live file, so the diff compares like
    with like (``sandbox_default``, ``data_root``).
    """
    diff = _devops_diff(registry, live_profiles_path, **load)
    default = registry.get_default()
    disabled = []
    for p in registry.profiles:
        if p.slug == default.slug:
            continue
        if p.enabled or p.slug in registry.boot_disabled:
            disabled.append(p.slug)
        registry.disable_for_run(p.slug, SAFE_MODE_REASON)
    registry.owner_only_for_run(default.slug)

    meta = _meta(layout)
    when = meta.get("ts") or "?"
    sha = meta.get("sha") or "?"
    models = f"models.yaml от {when}" if models_from_snapshot else "models.yaml живой"
    off = ", ".join(disabled) if disabled else "нет"
    msg = (
        f"🛟 Safe mode: бот падал подряд и запущен на последнем рабочем "
        f"конфиге от {when} (код {sha}). Работает только {default.slug}, "
        f"только для владельца. Выключены: {off}. {diff}. {models}. "
        f"Почини живой конфиг и /restart: проверка пройдёт — safe mode выключится."
    )
    logger.warning(
        "Safe mode boot", snapshot=when, sha=sha, disabled=disabled, diff=diff
    )
    return msg
